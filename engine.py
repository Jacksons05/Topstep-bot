"""The agentic futures-trading cycle: data -> quant -> agents -> confluence ->
risk -> execute -> manage. One run_once() = one full pass over the watchlist.

This is the Lucid funded-futures bot: it signals off the shared agentic core
(quant indicators + the LLM agent team) and executes through Rithmic with the
Lucid risk layer (EOD drawdown kill-switch, econ blackout, contract cap, EOD
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
from executor import build_executor
from marketdata import MarketData
from news import NewsFeed
from events import Events
from macro import Macro
from notifier import notify, signal_msg, trade_ticket, exit_ticket
from lucid_risk import hold_seconds
from regime import classify_last
from risk import check, circuit_breaker, kill_switch_active, should_exit
from signals import Signal, label_for, quant_signal
from state import State
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
        self._lucid_last_reset: date | None = None
        self._oflow = None  # RithmicOrderFlowFeed when a live Rithmic feed is up

        # ── Lucid / Rithmic mode ───────────────────────────────────────────
        # The Lucid bot executes futures through Rithmic with the Lucid risk
        # layer attached. When credentials are missing it degrades to the
        # base executor (Sim/Alpaca) so the agentic pipeline still runs in
        # paper — but it warns loudly, because live futures need Rithmic.
        self._lucid: "LucidRiskManager | None" = None  # type: ignore[name-defined]
        if CONFIG.lucid_mode_enabled and CONFIG.rithmic_user and CONFIG.rithmic_password:
            try:
                from rithmic_executor import RithmicBroker
                from lucid_risk import LucidRiskManager
                self.executor.broker = RithmicBroker()
                # Seed the Lucid drawdown base from the live account balance.
                # Falls back to bankroll_usd if the account call fails (mock mode).
                try:
                    acct = self.executor.broker.account()
                    initial_equity = acct.get("equity") or CONFIG.bankroll_usd
                except Exception:  # noqa: BLE001
                    initial_equity = CONFIG.bankroll_usd
                self._lucid = LucidRiskManager(initial_equity=initial_equity)
                notify(
                    "⚡ LUCID/RITHMIC MODE ACTIVE — "
                    f"system={CONFIG.rithmic_system} env={CONFIG.rithmic_env} | "
                    f"drawdown_limit=${CONFIG.lucid_daily_drawdown_usd:,.2f} | "
                    f"max_contracts={CONFIG.lucid_max_contracts} | "
                    f"flatten_at={CONFIG.lucid_flatten_time} ET"
                )
                # Live order-flow feed: stream BBO + trades off the same Rithmic
                # connection into per-symbol OBI/CVD/whale engines. Only the
                # futures roots in the watchlist get subscribed.
                if CONFIG.orderflow_gate_enabled:
                    try:
                        from rithmic_marketdata import RithmicOrderFlowFeed
                        self._oflow = RithmicOrderFlowFeed(self.executor.broker)
                        n = self._oflow.subscribe(list(CONFIG.watchlist))
                        notify(f"📡 Order-flow feed: subscribed {n} futures root(s) "
                               f"(OBI/CVD/whale gate {'live' if n else 'idle — no futures roots'})")
                    except Exception as e:  # noqa: BLE001
                        notify(f"⚠ Order-flow feed init failed ({e}) — gate disabled (fails open)")
                        self._oflow = None
            except Exception as e:  # noqa: BLE001
                notify(
                    f"⚠ LUCID MODE init failed ({e}) — "
                    "falling back to base executor. Check rithmic-python installation."
                )
                self._lucid = None
        else:
            notify(
                "⚠ Rithmic credentials not set (RITHMIC_USER/PASSWORD) — running the "
                "agentic pipeline on the base executor (Sim/paper). Set credentials in "
                ".env to trade futures live via Rithmic."
            )

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

    def close(self) -> None:
        self.data.close()
        self.news.close()
        self.macro.close()
        self.events.close()
        self.executor.close_broker()
        self.state.save()

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

        # Refresh Lucid drawdown base at the start of each new trading day
        if self._lucid is not None:
            today = date.today()
            if self._lucid_last_reset != today:
                try:
                    acct = self.executor.broker.account()
                    self._lucid.reset_day(acct.get("equity", CONFIG.bankroll_usd))
                    self._lucid_last_reset = today
                    if self._oflow is not None:
                        self._oflow.reset_session()  # CVD is a since-open running total
                except Exception:  # noqa: BLE001
                    pass

        # 0. reconcile provisional entries against real broker fills
        self._reconcile_fills()

        # 1. regime + circuit breaker
        regime_move = self.data.intraday_change_pct(CONFIG.regime_symbol)
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
            # Lucid risk layer: EOD flatten window, econ blackout, drawdown,
            # and contract cap — all checked BEFORE the base risk.check() call.
            if self._lucid is not None:
                lucid_ok, lucid_reason = self._lucid.pre_trade_ok(sig, self.state)
                if not lucid_ok:
                    notify(f"  skip (Lucid: {lucid_reason}): {sig.symbol}")
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
            # applied when the live Rithmic feed actually has data for this
            # symbol (fails open during warm-up / non-futures symbols).
            if self._oflow is not None:
                of = self._oflow.get(sig.symbol)
                if of.has_data:
                    of_ok, of_reason = of.confirm_entry(sig.side)
                    if not of_ok:
                        notify(f"  skip (order-flow: {of_reason}): {sig.symbol}")
                        continue
                    notify(f"  order-flow OK ({of_reason}): {sig.symbol}")

            qty = max(1, int(size / sig.price)) if sig.price > 0 else 0
            try:
                pos = self.executor.open(sig, size, self.state)
            except Exception as e:  # noqa: BLE001
                notify(f"  ! execute failed {sig.symbol}: {e}")
                continue
            self.cooldowns[sig.symbol] = time.time()
            notify("OPEN  " + signal_msg(sig, qty, size, self.executor.mode) +
                   f" [regime={rk} sz={regime_params['size_mult']:.0%}]")
            if CONFIG.manual_tickets and not pos.shadow:
                notify(trade_ticket(pos, sig.confidence, sig.confidence_label))

        self.state.save()

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
        quant = quant_signal(bars)
        if quant is None:
            return None
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

    # ── manage exits ──────────────────────────────────────
    def _manage_open(self) -> None:
        # Lucid EOD flatten: if the flatten window is open and the broker is
        # RithmicBroker, submit a market close for every open futures position.
        # This runs BEFORE the per-position exit loop so the state gets cleaned
        # up correctly on the same scan tick.
        if self._lucid is not None and self._lucid.should_flatten_now():
            from rithmic_executor import RithmicBroker
            if isinstance(self.executor.broker, RithmicBroker):
                open_futures = [p for p in self.state.open_positions if not p.shadow]
                if open_futures:
                    notify(
                        f"[Lucid] EOD flatten triggered ({len(open_futures)} positions) "
                        f"— submitting market closes via Rithmic"
                    )
                    try:
                        self.executor.broker.flatten_all()
                        for pos in open_futures:
                            q = self.data.quote(pos.symbol)
                            exit_price = q.price if q else pos.entry_price
                            self.state.close(pos, exit_price)
                            self._lucid.record_close(
                                pos.pnl_usd, hold_seconds(pos.opened_at)
                            )
                            notify(
                                f"  LUCID-FLATTEN {pos.symbol} qty={pos.qty} "
                                f"@ ~{exit_price:.2f}"
                            )
                            if CONFIG.manual_tickets:
                                notify(exit_ticket(pos, exit_price, "EOD flatten"))
                    except Exception as e:  # noqa: BLE001
                        notify(f"  ! Lucid flatten error: {e}")

        for pos in list(self.state.positions):
            if not pos.open:
                continue
            if not pos.filled:
                continue  # entry order still pending — nothing to exit yet
            q = self.data.quote(pos.symbol)
            if not q:
                continue
            reason = should_exit(pos, q.price)
            if not reason:
                continue
            # Lucid microscalp guard: defer a take-profit exit until the position
            # has been open ≥ min hold. Stop-losses are NEVER delayed (risk first).
            if (
                reason == "take-profit"
                and self._lucid is not None
                and not pos.shadow
                and not self._lucid.profit_exit_held_long_enough(pos.opened_at)
            ):
                notify(
                    f"  hold (Lucid <{CONFIG.lucid_min_profit_hold_sec:g}s): "
                    f"deferring take-profit {pos.symbol}"
                )
                continue
            try:
                self.executor.close(pos, q.price, self.state)
            except Exception as e:  # noqa: BLE001
                notify(f"  ! exit failed {pos.symbol}: {e}")
                continue
            if self._lucid is not None and not pos.shadow:
                self._lucid.record_close(pos.pnl_usd, hold_seconds(pos.opened_at))
            tag = "EXIT-SHADOW" if pos.shadow else "CLOSE"
            notify(f"{tag} {reason} {pos.symbol} @ {q.price:.2f} pnl=${pos.pnl_usd:.2f}")
            if CONFIG.manual_tickets and not pos.shadow:
                notify(exit_ticket(pos, q.price, reason))
