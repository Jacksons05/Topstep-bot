"""The agentic futures-trading cycle: data -> quant -> agents -> confluence ->
risk -> execute -> manage. One run_once() = one full pass over the watchlist.

This is the Topstep funded-futures bot: it signals off the shared agentic core
(quant indicators + the LLM agent team) and executes through ProjectX with the
Topstep risk layer (EOD drawdown kill-switch, econ blackout, contract cap, EOD
flatten). The equity-options path (Alpaca/CBOE GEX, 0DTE structures) has been
removed — see the sister repo Trading-Bot for that variant.

Confluence rule: a trade fires only when the quantitative stream (indicators)
and the qualitative stream (agent team) agree on direction AND the blended
confidence clears CONFIDENCE_THRESHOLD.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

from agents import AgentTeam, SymbolContext, portfolio_weights
from config import CONFIG
from executor import build_executor, futures_plan
from marketdata import MarketData
from news import NewsFeed
from events import Events
from macro import Macro
from notifier import notify, signal_msg, trade_ticket, exit_ticket
from topstep_risk import hold_seconds
from regime import classify_last
from risk import check, circuit_breaker, kill_switch_active, should_exit
from signals import Signal, label_for, quant_signal
from ml_signal import ML
from datafeed import LiveRecorder
from futures_symbols import dollar_value_per_point, is_futures_symbol
from state import Position, State
from regime_strategy import (
    get_regime_params,
    regime_allows_signal,
    apply_regime_sizing,
)


# US equity-market full-day holidays (NYSE), 2026. Used by the market-hours gate
# so the LLM scan doesn't burn API credits on closed days. (Index futures track
# the same RTH session the proxy data is sampled from.)
_US_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


class Engine:
    def __init__(self):
        self.data = MarketData()
        self.news = NewsFeed()
        self.macro = Macro()
        self.events = Events()
        self.team = AgentTeam()
        self.executor = build_executor()
        self.state = State.load()
        self.cooldowns: dict[str, float] = {}
        self.cb_level = "green"
        # cost control: skip re-running the LLM on a symbol whose quant read hasn't
        # moved since its last evaluation. {symbol: (direction, strength_bucket)}
        self._eval_cache: dict[str, tuple[str, float]] = {}
        self._topstep_last_reset: date | None = None
        # Topstep: True once a daily-loss / trailing-MLL breach has flattened the
        # book — blocks all new entries until the next session (cleared on reset).
        self._topstep_day_halt: bool = False
        # Fail-closed equity guard: True once a real broker equity read succeeds.
        # When a LIVE ProjectX equity read fails we block NEW entries (but keep
        # managing/flattening) rather than trade blind against the trailing MLL.
        self._equity_trusted: bool = True
        self._equity_blocked: bool = False
        # Aggressive equity estimate used to ratchet the trailing-MLL peak (set by
        # _account_equity each scan); seeded at the account size.
        self._peak_equity_est: float = CONFIG.topstep_account_size
        self._oflow = None  # ProjectXOrderFlowFeed when a live ProjectX feed is up
        self._uw = None     # UWFlowFeed when UW_FLOW_ENABLED=true + UW_API_KEY set
        # Record-own data layer (Databento alternative): captures bar+L2 snapshots
        # to parquet so the ML signal can be retrained on YOUR ProjectX feed.
        self.recorder = LiveRecorder() if CONFIG.record_data else None

        # ── Topstep / ProjectX (TopstepX) mode ───────────────────────────────
        # The Topstep bot executes futures through the ProjectX gateway with the
        # Topstep risk layer attached. When credentials are missing it degrades to
        # the base executor (Sim/Alpaca) so the agentic pipeline still runs in
        # paper — but it warns loudly, because live futures need ProjectX.
        self._topstep: "TopstepRiskManager | None" = None  # type: ignore[name-defined]
        if CONFIG.topstep_mode_enabled and CONFIG.projectx_username and CONFIG.projectx_api_key:
            try:
                from projectx_executor import ProjectXBroker
                from topstep_risk import TopstepRiskManager
                self.executor.broker = ProjectXBroker()
                # Seed the trailing-MLL peak / day base from the live balance.
                # Falls back to the account size if the call fails (mock mode).
                try:
                    acct = self.executor.broker.account()
                    initial_equity = acct.get("equity") or CONFIG.topstep_account_size
                except Exception:  # noqa: BLE001
                    initial_equity = CONFIG.topstep_account_size
                self._topstep = TopstepRiskManager(initial_equity=initial_equity)
                notify(
                    "⚡ TOPSTEP/PROJECTX MODE ACTIVE — "
                    f"env={'live' if CONFIG.projectx_live else 'sim'} | "
                    f"account=${CONFIG.topstep_account_size:,.0f} | "
                    f"trailing_MLL=${CONFIG.topstep_trailing_mll:,.0f} | "
                    f"daily_loss=${CONFIG.topstep_daily_loss_limit:,.0f} | "
                    f"max_contracts={CONFIG.topstep_max_contracts} | "
                    f"flatten_at={CONFIG.topstep_flatten_time} ET"
                )
                # Live order-flow feed: stream quotes + trades + depth off the
                # ProjectX market hub into per-symbol OBI/CVD/whale engines. Only
                # the futures roots in the watchlist get subscribed.
                if CONFIG.orderflow_gate_enabled:
                    try:
                        from projectx_marketdata import ProjectXOrderFlowFeed
                        self._oflow = ProjectXOrderFlowFeed(self.executor.broker)
                        n = self._oflow.subscribe(list(CONFIG.watchlist))
                        notify(f"📡 Order-flow feed: subscribed {n} futures root(s) "
                               f"(OBI/CVD/whale gate {'live' if n else 'idle — no futures roots'})")
                    except Exception as e:  # noqa: BLE001
                        notify(f"⚠ Order-flow feed init failed ({e}) — gate disabled (fails open)")
                        self._oflow = None
            except Exception as e:  # noqa: BLE001
                notify(
                    f"⚠ TOPSTEP MODE init failed ({e}) — "
                    "falling back to base executor. Check PROJECTX credentials / signalrcore."
                )
                self._topstep = None
        else:
            notify(
                "⚠ ProjectX credentials not set (PROJECTX_USERNAME/PROJECTX_API_KEY) — running "
                "the agentic pipeline on the base executor (Sim/paper). Set credentials in "
                ".env to trade futures live via ProjectX/TopstepX."
            )

        # Unusual Whales flow feed (optional) — options flow lean for futures proxies.
        if CONFIG.uw_flow_enabled and CONFIG.uw_api_key:
            try:
                from uw_flow import UWFlowFeed
                self._uw = UWFlowFeed()
                notify("🐋 Unusual Whales flow feed enabled "
                       f"(lean_weight={CONFIG.uw_flow_lean_weight:.0%} | "
                       f"cache={CONFIG.uw_flow_cache_sec}s | "
                       f"whale≥${CONFIG.uw_whale_premium_usd:,.0f})")
            except Exception as e:  # noqa: BLE001
                notify(f"⚠ UW flow feed init failed ({e}) — disabled")
                self._uw = None

        # UW signal-quality logger (optional) — records uw_lean vs forward return
        # so the blend's edge can be measured offline. Behavior-neutral.
        self._uw_log = None
        if CONFIG.uw_flow_log:
            try:
                from uw_logger import UWFlowLogger
                self._uw_log = UWFlowLogger(CONFIG.uw_flow_log)
                if self._uw_log.enabled:
                    notify(f"📊 UW signal logger → {CONFIG.uw_flow_log} "
                           "(analyze: python uw_logger.py)")
            except Exception as e:  # noqa: BLE001
                notify(f"⚠ UW logger init failed ({e}) — disabled")
                self._uw_log = None

        # Startup reconciliation (H6): adopt/drop positions against the live broker
        # so a restart after a Topstep auto-liquidation never manages ghosts.
        try:
            self._reconcile_positions()
        except Exception as e:  # noqa: BLE001
            notify(f"⚠ startup reconcile failed ({e})")

    def _market_open(self) -> bool:
        """True during US regular trading hours (Mon–Fri 9:30–16:00 ET, skip
        holidays). Always True when MARKET_HOURS_ONLY is off."""
        if not CONFIG.market_hours_only:
            return True
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5 or now.date().isoformat() in _US_HOLIDAYS:
            return False
        mins = now.hour * 60 + now.minute
        return 9 * 60 + 30 <= mins < 16 * 60

    def _topstep_session_date(self) -> date:
        """Date of the current CME/Topstep trading session. The futures session
        rolls at 18:00 ET, so trades placed at/after 18:00 ET belong to the NEXT
        calendar day's session. Used to anchor the daily-loss / consistency reset
        to the real session boundary instead of the server's midnight."""
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        d = now.date()
        if now.hour >= 18:
            d = d + timedelta(days=1)
        return d

    def close(self) -> None:
        self.data.close()
        self.news.close()
        self.macro.close()
        self.events.close()
        self.executor.close_broker()
        self.state.save()
        if self._uw is not None:
            self._uw.close()
        if self._uw_log is not None:
            self._uw_log.close()

    # ── adaptive cadence: poll faster when the breaker trips, idle when closed ──
    def next_interval(self) -> int:
        if not self._market_open():
            return CONFIG.closed_interval_sec
        return CONFIG.fast_interval_sec if self.cb_level != "green" else CONFIG.scan_interval_sec

    # ── one full cycle ────────────────────────────────────
    def run_once(self) -> None:
        # Cost gate: outside market hours, do nothing (no fills happen, no LLM spend).
        if not self._market_open():
            notify("market closed — idle (no scan)")
            return

        # Kill switch (M14): a disk file / env flag instantly FLATTENS every open
        # position and halts management+entries for this cycle — not merely blocks
        # new entries. Checked before anything else so a panic-stop takes effect now.
        if kill_switch_active():
            self._kill_switch_halt()
            return

        # Reconcile the local book against the broker's real open positions (H6)
        # BEFORE managing anything: drop phantoms Topstep auto-liquidated / closed,
        # adopt orphans, so we never manage a position that no longer exists.
        self._reconcile_positions()

        # Refresh the Topstep day base (Daily Loss Limit anchor) each new session.
        # Keyed off the CME/Topstep SESSION date (rolls 18:00 ET), NOT the server
        # UTC/local calendar date — otherwise the daily-loss + consistency limits
        # reset mid-session and let the bot re-risk after it should be done.
        if self._topstep is not None:
            today = self._topstep_session_date()
            if self._topstep_last_reset != today:
                try:
                    acct = self.executor.broker.account()
                    self._topstep.reset_day(acct.get("equity", CONFIG.bankroll_usd))
                    self._topstep_last_reset = today
                    self._topstep_day_halt = False  # new session — limits reset
                    if self._oflow is not None:
                        self._oflow.reset_session()  # CVD is a since-open running total
                except Exception:  # noqa: BLE001
                    pass

            # Topstep account protection: ratchet the trailing-MLL peak on the
            # live equity, then hard-flatten + halt the day on any breach
            # (trailing Max Loss Limit = account fail; Daily Loss Limit = day off).
            equity, unrealized = self._account_equity()
            if self._live_projectx() and not self._equity_trusted:
                # Fail-closed: couldn't read real equity on a live account. Don't
                # ratchet the MLL peak on a guessed number and don't open new
                # trades this cycle — but keep managing/flattening open positions.
                self._equity_blocked = True
                notify("⚠ Topstep: live equity read failed — fail-closed "
                       "(no new entries this cycle; open positions still managed)")
            else:
                self._equity_blocked = False
                # Ratchet the trailing-MLL peak off the aggressive live estimate;
                # check the breach off the conservative equity.
                self._topstep.update_equity(self._peak_equity_est)
                breached, why = self._topstep.risk_breach(equity, self.state, unrealized)
                if breached and not self._topstep_day_halt:
                    notify(f"🛑 TOPSTEP BREACH — {why} | flattening all & halting new entries")
                    self._topstep_flatten_all(why)
                    self._topstep_day_halt = True

        # 0. reconcile provisional entries against real broker fills
        self._reconcile_fills()

        # 1. regime + circuit breaker. A None move = data outage (NOT a genuine 0%
        #    move) → fail CLOSED: force the breaker RED so no new entries fire blind
        #    during a feed blackout (open positions are still managed/flattened).
        regime_move = self.data.intraday_change_pct(CONFIG.regime_symbol)
        if regime_move is None:
            self.cb_level, cb_mult = "red", 0.0
            notify("⚠ regime data unavailable — circuit breaker RED (fail closed, no new entries)")
        else:
            self.cb_level, cb_mult = circuit_breaker(regime_move)

        # 2. manage exits first
        self._manage_open()

        # 3. two-stage funnel so the universe can be large without LLM cost exploding.
        held = {p.symbol for p in self.state.open_positions}

        # 3a. cheap quant prescreen across the WHOLE watchlist (no LLM)
        prescreened = []  # (sym, quant, spot, regime_label)
        for sym in CONFIG.watchlist:
            if sym in held:
                continue
            ps = self._prescreen(sym)
            if ps is not None:
                prescreened.append((sym, ps[0], ps[1], ps[2]))
        # rank by quant conviction; only the strongest get the expensive LLM pass
        prescreened.sort(key=lambda x: x[1].strength, reverse=True)
        top = prescreened[:CONFIG.llm_max_symbols_per_cycle]

        # 3b. full eval (LLM confluence) on the top-N candidates only
        candidates: list[tuple[Signal, float, dict]] = []  # (signal, conviction, regime_params)
        regime_str = "—"
        for sym, quant, spot, regime_label in top:
            # cost control: skip the LLM team when this symbol's quant read hasn't
            # moved since we last evaluated it (same direction + strength bucket).
            key = (quant.direction, round(quant.strength / 0.05) * 0.05)
            if self._eval_cache.get(sym) == key:
                continue
            self._eval_cache[sym] = key
            sig = self._evaluate_full(sym, quant, spot)
            if sig:
                rp = get_regime_params(regime_label, CONFIG)
                candidates.append((sig, sig.confidence, rp))
                if regime_str == "—":
                    regime_str = regime_label

        notify(f"scan: {len(CONFIG.watchlist)} universe | "
               f"prescreen={len(prescreened)} scored={len(top)} | "
               f"open={len(self.state.open_positions)} | "
               f"realized=${self.state.realized_pnl_usd:.2f} | "
               f"dayPnL=${self.state.daily_pnl():.2f} | "
               f"cb={self.cb_level} | regime={regime_str}")

        # 4. portfolio allocation across agreeing candidates
        weights = portfolio_weights([(s.symbol, c) for s, c, _ in candidates])

        # 5. risk gate + execute.
        if candidates and self._past_entry_cutoff():
            notify(f"  skip ALL new entries (entry cutoff {CONFIG.entry_cutoff_et} ET) — "
                   f"{len(candidates)} candidate(s); managing open positions only")
            candidates = []
        for sig, conv, regime_params in sorted(
                candidates, key=lambda x: x[1], reverse=True):
            w = weights.get(sig.symbol, 0.0)

            # ── Regime-adaptive playbook gates ──────────────────────────
            rk = regime_params.get("regime_key", "UNKNOWN")

            # (a) Direction gate — e.g. Crisis blocks new longs
            dir_ok, dir_reason = regime_allows_signal(regime_params, sig.side)
            if not dir_ok:
                notify(f"  skip ({dir_reason}): {sig.symbol}")
                continue

            # (b) Per-regime minimum confidence (stricter than global threshold)
            regime_min_conf = regime_params.get("min_conf", CONFIG.confidence_threshold)
            if sig.confidence < regime_min_conf:
                notify(f"  skip (regime {rk} conf {sig.confidence:.2f} < {regime_min_conf:.2f}): "
                       f"{sig.symbol}")
                continue

            # (c) Per-regime concurrent-position cap (e.g. Crisis: max 2)
            regime_max_pos = regime_params.get("max_positions", CONFIG.max_concurrent)
            if len(self.state.open_positions) >= regime_max_pos:
                notify(f"  skip (regime {rk} max_positions={regime_max_pos} reached): "
                       f"{sig.symbol}")
                continue

            black, why = self.events.blackout(sig.symbol)
            if black:
                notify(f"  skip (event blackout: {why}): {sig.symbol}")
                continue
            # Topstep risk layer: flatten window, econ blackout, trailing MLL,
            # daily loss limit, account-wide contract cap, consistency cap —
            # all checked BEFORE the base risk.check() call.
            if self._topstep is not None:
                if self._topstep_day_halt:
                    notify(f"  skip (Topstep: day halted by risk breach): {sig.symbol}")
                    continue
                if self._equity_blocked:
                    notify(f"  skip (Topstep: equity read failed — fail-closed): {sig.symbol}")
                    continue
                equity, unrealized = self._account_equity()
                topstep_ok, topstep_reason = self._topstep.pre_trade_ok(
                    sig, self.state, equity, unrealized)
                if not topstep_ok:
                    notify(f"  skip (Topstep: {topstep_reason}): {sig.symbol}")
                    continue
            ok, size, reason = check(sig, self.state, cb_mult=cb_mult * (w or 1.0),
                                     cooldowns=self.cooldowns)
            if not ok:
                notify(f"  skip ({reason}): {sig.symbol}")
                continue

            # (d) Regime sizing multiplier applied AFTER the base risk.check()
            #     so the min-size guard still runs against the un-scaled base.
            size = apply_regime_sizing(size, regime_params)
            if size < CONFIG.min_executable_size_usd:
                notify(f"  skip (regime {rk} sized to {size:.0f} < min "
                       f"{CONFIG.min_executable_size_usd:.0f}): {sig.symbol}")
                continue

            # ── Order-flow confirmation gate ────────────────────────────
            # Final microstructure check at entry: require OBI extreme in the
            # trade direction, veto on opposing whale / CVD divergence. Only
            # applied when the live ProjectX feed actually has data for this
            # symbol (fails open during warm-up / non-futures symbols).
            if self._oflow is not None:
                of = self._oflow.get(sig.symbol)
                if of.has_data:
                    of_ok, of_reason = of.confirm_entry(sig.side)
                    if not of_ok:
                        notify(f"  skip (order-flow: {of_reason}): {sig.symbol}")
                        continue
                    notify(f"  order-flow OK ({of_reason}): {sig.symbol}")
                elif of.stale:
                    # Feed HAD data but is now frozen → fail CLOSED: do not enter on
                    # stale microstructure (a warm-up/cold feed still fails open above).
                    notify(f"  skip (order-flow feed stale — fail closed): {sig.symbol}")
                    continue

            # ── Risk-based futures sizing + worst-case MLL pre-check (C3/H7) ──
            # For a futures symbol, size off the per-trade risk budget and reject
            # outright if (a) the stop is too wide for the budget (qty=0) or (b) the
            # full stop-out would breach the LIVE trailing-MLL floor.
            if is_futures_symbol(sig.symbol):
                plan = futures_plan(sig, sig.price)
                if plan is None:
                    notify(f"  skip (sizing: stop too wide / invalid ATR for risk budget): {sig.symbol}")
                    continue
                if self._topstep is not None:
                    eq_chk, _u = self._account_equity()
                    if (eq_chk - plan.risk_usd) <= self._topstep.mll_floor():
                        notify(f"  skip (worst-case loss ${plan.risk_usd:,.0f} would breach "
                               f"trailing MLL floor ${self._topstep.mll_floor():,.0f}): {sig.symbol}")
                        continue

            try:
                pos = self.executor.open(sig, size, self.state)
            except Exception as e:  # noqa: BLE001
                notify(f"  ! execute failed {sig.symbol}: {e}")
                continue
            if pos is None:
                notify(f"  skip (sizing rejected — no order placed): {sig.symbol}")
                continue
            qty = pos.qty
            self.cooldowns[sig.symbol] = time.time()
            notify("OPEN  " + signal_msg(sig, qty, size, self.executor.mode) +
                   f" [regime={rk} sz={regime_params['size_mult']:.0%}]")
            if CONFIG.manual_tickets and not pos.shadow:
                notify(trade_ticket(pos, sig.confidence, sig.confidence_label))

        self.state.save()

    # ── live order-flow → ML micro-feature dict ──
    def _micro_for(self, sym: str) -> dict | None:
        """Snapshot the per-symbol order-flow engine into the micro feature dict
        features.feature_row expects. None when no live L2/trades for this name."""
        if self._oflow is None:
            return None
        of = self._oflow.get(sym)
        if of is None or not getattr(of, "has_data", False):
            return None
        return {
            "obi": of.obi, "cvd": of.cvd, "micro_price": of.micro_price,
            "bid": of.bid, "ask": of.ask, "whale": of.whale(),
            "cvd_div": of.cvd_divergence(),
        }

    # ── stage 1: cheap quant prescreen (no LLM/news) ──
    def _prescreen(self, sym: str):
        """Fast pass run on the WHOLE universe: bars + indicators only.

        Returns (quant, spot, regime_label) for a passable read, else None.
        No LLM, no network beyond bars.
        """
        bars = self.data.bars(sym, timeframe=CONFIG.scalp_timeframe, limit=200)
        if not bars.get("close"):
            return None
        spot = bars["close"][-1]
        micro = self._micro_for(sym)
        if self.recorder is not None:
            self.recorder.record(sym, bars, self._oflow.get(sym) if self._oflow else None)
        # ML quant signal first (LightGBM on bar + order-flow features); fall back
        # to the deterministic indicator model when no model is loaded or it has
        # no opinion. Same QuantRead contract either way.
        quant = ML.read(bars, micro) if (CONFIG.ml_signal_enabled and ML.ready) else None
        if quant is None:
            quant = quant_signal(bars)
        if quant is None:
            return None

        # Blend Unusual Whales options-flow lean into the quant read.
        # UW lean is derived from net call-vs-put premium on the futures proxy
        # (ES→SPX, NQ→NDX). A weight of 0.30 means 70% indicators + 30% UW flow.
        if self._uw is not None:
            uw = self._uw.get(sym)
            if uw is not None:
                raw_quant_lean = quant.lean  # pre-blend, for the signal logger
                w = CONFIG.uw_flow_lean_weight
                blended = (1.0 - w) * quant.lean + w * uw.lean
                blended = max(-1.0, min(1.0, blended))
                from signals import QuantRead
                whale_tag = " 🐋" if uw.whale else ""
                quant = QuantRead(
                    lean=round(blended, 4),
                    strength=round(abs(blended), 4),
                    atr=quant.atr,
                    detail=quant.detail + f" · UW={uw.lean:+.2f}{whale_tag}",
                )
                if self._uw_log is not None:
                    self._uw_log.log(sym, spot, uw.lean, raw_quant_lean)

        if quant.direction == "FLAT":
            return None

        # Classify regime for this symbol (used both by the gate and later by
        # the regime-adaptive playbook in run_once).
        regime_label = "Unknown"
        if CONFIG.regime_gate_enabled:
            regime_label = classify_last(
                bars["close"],
                bars.get("high") or bars["close"],
                bars.get("low") or bars["close"],
            )
            reg_upper = regime_label.upper()
            if CONFIG.regime_allow:
                if reg_upper not in CONFIG.regime_allow:
                    return None
            elif CONFIG.regime_block and reg_upper in CONFIG.regime_block:
                return None

        return quant, spot, regime_label

    # ── stage 2: full eval (LLM confluence) on top-N ──
    def _evaluate_full(self, sym: str, quant, spot: float) -> Signal | None:
        # Use the LIVE quote for the trade spot (entry ref, stop/target).
        # The last bar close can lag/stale.
        q = self.data.quote(sym)
        if q and q.price > 0:
            spot = q.price

        # Headlines only matter when an LLM agent will read them — skip the
        # news call on the quant-only path to save rate limit.
        headlines = self.news.headlines(sym) if self.team.ready else []
        if self.team.ready and self._uw is not None:
            headlines = self._uw.headlines(sym) + headlines

        ctx = SymbolContext(
            symbol=sym, spot=spot, quant_detail=quant.detail, quant_lean=quant.lean,
            exposure=None, news=headlines,
            macro=self.macro.line() if self.team.ready else "",
        )
        verdict = self.team.evaluate(ctx)

        # When no LLM is configured the qualitative lean is 0 — fall back to a
        # quant-only signal so the bot still trades (paper) while you wire keys.
        if not self.team.ready:
            qual_dir = quant.direction
            qual_conv = quant.strength
        else:
            qual_dir = verdict.direction
            qual_conv = verdict.conviction
            if qual_dir == "HOLD":
                return None

        # confluence: directions must agree
        if qual_dir != quant.direction:
            return None

        confidence = round(0.5 * quant.strength + 0.5 * qual_conv, 3)
        if confidence < CONFIG.confidence_threshold:
            return None

        agents = dict(verdict.trail)

        # quant-only mode has no narrative thesis — surface the indicator read.
        thesis = quant.detail if not self.team.ready else (verdict.thesis or quant.detail)
        return Signal(
            symbol=sym, asset="future", side=quant.direction, price=spot,
            confidence=confidence, confidence_label=label_for(confidence),
            thesis=thesis, quant=quant.lean,
            qual=verdict.qual_lean, atr=quant.atr, agents=agents,
        )

    # ── reconcile provisional entries with real fills ─────
    def _reconcile_fills(self) -> None:
        """Provisional entries (recorded at the reference price when the order
        was merely 'accepted') get rewritten to the true fill price once the
        broker reports it. ATR stop/target distances are preserved by shifting
        them with the entry. Dead orders are dropped.
        """
        for pos in self.state.open_positions:  # real book only; shadow has no order
            if pos.filled or not pos.order_id or pos.order_id == "sim":
                continue
            try:
                status, price = self.executor.get_fill(pos.order_id)
            except Exception:  # noqa: BLE001
                continue
            if status in ("canceled", "expired", "rejected"):
                pos.open = False
                pos.filled = True
                pos.exit_price = pos.entry_price
                pos.pnl_usd = 0.0
                pos.closed_at = datetime.now(timezone.utc).isoformat()
                notify(f"VOID {pos.symbol} order {status} — provisional entry dropped")
                continue
            if status == "filled" and price:
                s_off = pos.stop - pos.entry_price
                t_off = pos.target - pos.entry_price
                pos.entry_price = price
                pos.stop = price + s_off
                pos.target = price + t_off
                pos.size_usd = pos.qty * price
                pos.filled = True
                notify(f"FILL {pos.symbol} @ {price:.2f} (reconciled from provisional)")

    def _past_entry_cutoff(self) -> bool:
        """True once we're at/after ENTRY_CUTOFF_ET. Blocks NEW entries only;
        open positions are still managed and flattened normally."""
        from zoneinfo import ZoneInfo
        try:
            hh, mm = (int(x) for x in CONFIG.entry_cutoff_et.split(":"))
        except (ValueError, AttributeError):
            return False
        now = datetime.now(ZoneInfo("America/New_York"))
        return (now.hour, now.minute) >= (hh, mm)

    # ── Topstep equity / breach flatten ───────────────────
    def _mark(self, symbol: str) -> float | None:
        """Live mark for a symbol. Prefers the ProjectX order-flow feed's
        liquidity-weighted micro-price (a REAL futures price) over the Alpaca
        stocks quote, which returns nothing for futures roots (ES/NQ/…). Falls
        back to the BBO mid, then the Alpaca quote (equities/sim). None when no
        price is available — callers must skip marking rather than assume 0."""
        if self._oflow is not None:
            of = self._oflow.get(symbol)
            if of is not None and getattr(of, "has_data", False):
                mp = of.micro_price
                if mp and mp > 0:
                    return float(mp)
                if of.bid and of.ask:
                    return (of.bid + of.ask) / 2
        q = self.data.quote(symbol)
        return q.price if q and q.price > 0 else None

    def _live_projectx(self) -> bool:
        """True only when executing against a real (non-mock) ProjectX account."""
        b = self.executor.broker
        return b.__class__.__name__ == "ProjectXBroker" and not getattr(b, "_mock_mode", True)

    def _account_equity(self) -> tuple[float, float]:
        """Best estimate of live account equity for the trailing-MLL / daily-loss
        guards, plus the open-position unrealized P&L.

        Unrealized is marked with the live futures price (_mark) and scaled to
        DOLLARS by the contract multiplier — the same way state.py books realized
        P&L — so the two stay consistent. Equity is the CONSERVATIVE (lower) of
        the bot's internal book (account_size + realized + unrealized) and the
        live broker balance + open MTM, giving a real-time combined-equity view
        that a stale balance can't inflate. Sets self._equity_trusted=True only
        when a real broker balance was read (drives the fail-closed entry gate).
        """
        unrealized = 0.0
        for p in self.state.open_positions:
            if p.shadow:
                continue
            mark = self._mark(p.symbol)
            if mark is None:
                continue
            direction = 1.0 if p.side == "BUY" else -1.0
            mult = dollar_value_per_point(p.symbol)
            unrealized += (mark - p.entry_price) * p.qty * direction * mult

        internal_equity = (CONFIG.topstep_account_size
                           + self.state.realized_pnl_usd + unrealized)
        equity = internal_equity
        # Peak estimate for the trailing-MLL high-water-mark. Topstep ratchets its
        # floor on the HIGHER (more aggressive) live unrealized peak, so for the
        # PEAK we take the max of the two equity views — under-ratcheting the peak
        # would set our floor BELOW Topstep's real floor and let us breach theirs
        # while we still think we have room. The breach CHECK still uses the
        # conservative (min) equity below.
        peak_est = internal_equity
        self._equity_trusted = False
        try:
            acct = self.executor.broker.account()
            src = str(acct.get("source", ""))
            bal = acct.get("equity")
            if bal and "mock" not in src and "error" not in src:
                # Real-time combined equity = realized cash balance + open MTM.
                equity = min(internal_equity, float(bal) + unrealized)
                peak_est = max(internal_equity, float(bal) + unrealized)
                self._equity_trusted = True
        except Exception:  # noqa: BLE001
            pass
        self._peak_equity_est = peak_est
        return equity, unrealized

    def _flatten_all(self, reason: str) -> None:
        """Broker-agnostic flatten of every open real position via the executor
        (used by the kill switch when no ProjectX/Topstep layer is attached)."""
        for pos in list(self.state.open_positions):
            if pos.shadow:
                continue
            mark = self._mark(pos.symbol)
            exit_price = mark if mark is not None else pos.entry_price
            try:
                self.executor.close(pos, exit_price, self.state)
                if self._topstep is not None:
                    self._topstep.record_close(pos.pnl_usd, hold_seconds(pos.opened_at))
                notify(f"  FLATTEN ({reason}) {pos.symbol} qty={pos.qty} @ ~{exit_price:.2f}")
            except Exception as e:  # noqa: BLE001
                notify(f"  ! flatten error {pos.symbol}: {e}")

    def _kill_switch_halt(self) -> None:
        """M14: kill switch — flatten ALL positions and halt management for the
        cycle (the base risk.check only blocks NEW entries)."""
        notify("🛑 KILL SWITCH active — flattening ALL positions and halting management")
        if self._topstep is not None:
            self._topstep_flatten_all("kill switch")
            self._topstep_day_halt = True
        else:
            self._flatten_all("kill switch")
        self.state.save()

    def _reconcile_positions(self) -> None:
        """H6: sync the local book against the broker's actual open positions.

        Phantom (local-open, not at broker) → closed locally (Topstep
        auto-liquidation or a manual/native-stop close) so the engine stops
        managing it. Orphan (at broker, not local) → adopted as a managed
        Position so it is counted and EOD-flattened. Only runs against a real
        (non-mock) ProjectX account."""
        broker = self.executor.broker
        get_positions = getattr(broker, "get_positions", None)
        if not callable(get_positions) or not self._live_projectx():
            return
        try:
            broker_positions = get_positions()
        except Exception as e:  # noqa: BLE001
            notify(f"⚠ reconcile: broker.get_positions() failed ({e}) — skipped")
            return

        bro: dict[str, dict] = {}
        for bp in broker_positions or []:
            sym = str(bp.get("symbol", "")).upper()
            if sym:
                bro[sym] = bp

        changed = False
        # phantom: local open (non-shadow) with no matching broker position
        for pos in list(self.state.open_positions):
            if pos.shadow:
                continue
            if pos.symbol.upper() not in bro:
                mark = self._mark(pos.symbol)
                exit_price = mark if mark is not None else pos.entry_price
                self.state.close(pos, exit_price)
                if self._topstep is not None:
                    self._topstep.record_close(pos.pnl_usd, hold_seconds(pos.opened_at))
                notify(f"♻ reconcile: phantom {pos.symbol} not at broker — closed "
                       f"locally @ ~{exit_price:.2f} pnl=${pos.pnl_usd:.2f}")
                changed = True

        # orphan: broker position with no local counterpart → adopt
        local_syms = {p.symbol.upper() for p in self.state.open_positions if not p.shadow}
        for sym, bp in bro.items():
            if sym in local_syms:
                continue
            qty = float(bp.get("qty") or 0.0)
            if qty <= 0:
                continue
            entry = float(bp.get("avg_price") or 0.0) or (self._mark(sym) or 0.0)
            side = bp.get("side", "BUY")
            self.state.add(Position(
                symbol=sym, asset="future", side=side, qty=qty,
                entry_price=entry, size_usd=entry * qty, stop=0.0, target=0.0,
                kind="adopted", thesis="reconciled orphan (broker position)",
                opened_at=datetime.now(timezone.utc).isoformat(),
                mode=self.executor.mode, order_id="", filled=True,
            ))
            notify(f"⚠ reconcile: ORPHAN broker position {sym} {side} qty={qty} "
                   f"adopted (no protective stop attached — will EOD-flatten; review)")
            changed = True

        if changed:
            self.state.save()

    def _topstep_flatten_all(self, reason: str) -> None:
        """Market-close every open real position via ProjectX and book it in
        state. Used by the breach handler and the EOD flatten window."""
        from projectx_executor import ProjectXBroker
        if not isinstance(self.executor.broker, ProjectXBroker):
            return
        open_futures = [p for p in self.state.open_positions if not p.shadow]
        if not open_futures:
            return
        notify(f"[Topstep] flatten ({reason}) — {len(open_futures)} position(s) "
               f"via ProjectX")
        try:
            self.executor.broker.flatten_all()
            for pos in open_futures:
                mark = self._mark(pos.symbol)
                exit_price = mark if mark is not None else pos.entry_price
                self.state.close(pos, exit_price)
                self._topstep.record_close(pos.pnl_usd, hold_seconds(pos.opened_at))
                notify(f"  TOPSTEP-FLATTEN {pos.symbol} qty={pos.qty} @ ~{exit_price:.2f}")
                if CONFIG.manual_tickets:
                    notify(exit_ticket(pos, exit_price, reason))
        except Exception as e:  # noqa: BLE001
            notify(f"  ! Topstep flatten error: {e}")

    # ── manage exits ──────────────────────────────────────
    def _manage_open(self) -> None:
        # Topstep EOD flatten: when the flatten window is open (≥ 16:08 ET, before
        # the 16:10 futures close) close every open position so no unrealized risk
        # is carried against the trailing MLL overnight. Runs BEFORE the per-position
        # exit loop so state is cleaned up on the same scan tick.
        if self._topstep is not None and self._topstep.should_flatten_now():
            self._topstep_flatten_all("EOD flatten")

        for pos in list(self.state.positions):
            if not pos.open:
                continue
            if not pos.filled:
                continue  # entry order still pending — nothing to exit yet
            mark = self._mark(pos.symbol)
            if mark is None:
                continue  # no live price (futures need the ProjectX feed) — can't manage
            reason = should_exit(pos, mark)
            if not reason:
                continue
            # Topstep microscalp guard: defer a take-profit exit until the position
            # has been open ≥ min hold. Stop-losses are NEVER delayed (risk first).
            if (
                reason == "take-profit"
                and self._topstep is not None
                and not pos.shadow
                and not self._topstep.profit_exit_held_long_enough(pos.opened_at)
            ):
                notify(
                    f"  hold (Topstep <{CONFIG.topstep_min_profit_hold_sec:g}s): "
                    f"deferring take-profit {pos.symbol}"
                )
                continue
            try:
                self.executor.close(pos, mark, self.state)
            except Exception as e:  # noqa: BLE001
                notify(f"  ! exit failed {pos.symbol}: {e}")
                continue
            if self._topstep is not None and not pos.shadow:
                self._topstep.record_close(pos.pnl_usd, hold_seconds(pos.opened_at))
            tag = "EXIT-SHADOW" if pos.shadow else "CLOSE"
            exit_px = pos.exit_price if pos.exit_price is not None else mark
            notify(f"{tag} {reason} {pos.symbol} @ {exit_px:.2f} pnl=${pos.pnl_usd:.2f}")
            if CONFIG.manual_tickets and not pos.shadow:
                notify(exit_ticket(pos, exit_px, reason))
