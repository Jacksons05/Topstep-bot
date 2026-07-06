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

import math
import time
from datetime import date, datetime, timezone
import day_learner as _day_learner

from agents import AgentTeam, SymbolContext, portfolio_weights
from config import CONFIG
from executor import build_executor, futures_plan
from marketdata import MarketData
from news import NewsFeed
from events import Events
from macro import Macro
from notifier import notify, signal_msg, trade_ticket, exit_ticket
from topstep_risk import hold_seconds
from regime import classify_atr_percentile, classify_last
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
        # Apply yesterday's EOD adaptations at startup (conf threshold, size mult, etc.)
        _day_learner.apply_to_config(CONFIG, _day_learner.load_adapt(), notify)
        self.cb_level = "green"
        # EOD learning: fired once per calendar day on first market-close tick.
        self._day_learn_fired: str = ""
        # cost control: skip re-running the LLM on a symbol whose quant read hasn't
        # moved since its last evaluation. {symbol: (direction, strength_bucket)}
        self._eval_cache: dict[str, tuple[str, float]] = {}
        self._eval_cache_ts: dict[str, float] = {}   # last full-eval timestamp per symbol
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
        # Last trusted broker equity reading. Used instead of the internal book
        # when the local state has no recorded trading history for this account
        # (realized_pnl_usd == 0), so a fresh/blank local DB doesn't clamp a real,
        # already-profitable broker account down to the raw account_size floor.
        self._last_good_broker_equity: float | None = None
        # Broker-synced ledger anchor: broker_cash − local_realized captured on
        # the first trusted balance read. The local DB rarely holds the account's
        # full lifetime history, so account_size + realized is a phantom ledger —
        # anchoring to the broker makes the internal cross-check honest instead
        # of manufacturing false MLL breaches after the first local close.
        self._equity_baseline: float | None = None
        self._oflow = None  # ProjectXOrderFlowFeed when a live ProjectX feed is up
        self._uw = None     # UWFlowFeed when UW_FLOW_ENABLED=true + UW_API_KEY set
        # Partial-exit / BE-ratchet state keyed by position symbol.
        # {symbol: {"partial_done": bool, "be_done": bool, "trail_peak": float}}
        self._be_state: dict[str, dict] = {}
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
                # Restore the persisted session anchors (DLL day base, MLL peak,
                # day-halt) so a restart can't re-arm a fresh daily allowance.
                _sess = str(self._topstep_session_date())
                _restored, _halt = self._topstep.load_day_state(_sess)
                if _restored:
                    self._topstep_last_reset = self._topstep_session_date()
                    if _halt:
                        self._topstep_day_halt = True
                        notify("🛑 Topstep: DAY HALT restored from persisted "
                               "session state — no new entries until next session")
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

        # Startup reconciliation (H6): adopt/drop positions against the live broker
        # so a restart after a Topstep auto-liquidation never manages ghosts.
        try:
            self._reconcile_positions()
        except Exception as e:  # noqa: BLE001
            notify(f"⚠ startup reconcile failed ({e})")

    def _market_open(self) -> bool:
        """True during CME futures trading hours.

        CME Globex session: Sun 18:00 ET → Fri 17:00 ET, with a daily
        maintenance break 17:00–18:00 ET. Saturday is always closed.
        Always True when MARKET_HOURS_ONLY is off.
        """
        if not CONFIG.market_hours_only:
            return True
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        wd = now.weekday()   # Mon=0 … Sun=6
        mins = now.hour * 60 + now.minute
        # Saturday: always closed
        if wd == 5:
            return False
        # Sunday: open only after 18:00 ET (Globex Sunday open)
        if wd == 6:
            return mins >= 18 * 60
        # Mon–Fri: closed during 17:00–18:00 ET maintenance window
        return not (17 * 60 <= mins < 18 * 60)

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
            # EOD learning: fire once per calendar day on first closed-market tick.
            _today = str(date.today())
            if self._day_learn_fired != _today:
                self._day_learn_fired = _today
                try:
                    _day_learner.trigger("topstep", cfg=CONFIG, also_retrain=False)
                except Exception as _e:  # noqa: BLE001
                    notify(f"⚠ day_learner failed: {_e}")
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
                    src = str(acct.get("source", ""))
                    bal = acct.get("equity")
                    if not bal or "mock" in src or "error" in src:
                        # Failed/mock balance read at the session roll: do NOT
                        # anchor the Daily Loss Limit to a fallback figure (a
                        # $50k anchor on a $99k account disarms the DLL for the
                        # whole session). Retry next scan.
                        raise RuntimeError(f"untrusted equity read ({src})")
                    self._topstep.reset_day(float(bal))
                    self._topstep_last_reset = today
                    self._topstep_day_halt = False  # new session — limits reset
                    self._topstep.save_day_state(str(today), False)
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
                prev_peak = self._topstep.peak_equity
                self._topstep.update_equity(self._peak_equity_est)
                if self._topstep.peak_equity > prev_peak:
                    # Persist every new high: a restart must never restore a
                    # lower peak (= looser MLL floor) than Topstep has locked.
                    self._topstep.save_day_state(str(today), self._topstep_day_halt)
                breached, why = self._topstep.risk_breach(equity, self.state, unrealized)
                if breached and not self._topstep_day_halt:
                    notify(f"🛑 TOPSTEP BREACH — {why} | flattening all & halting new entries")
                    self._topstep_flatten_all(why)
                    self._topstep_day_halt = True
                    self._topstep.save_day_state(str(today), True)
                self._topstep.check_combine_progress(self.state)

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
        # Day-adapt: hour block + symbol cooldown check before any LLM work.
        from zoneinfo import ZoneInfo as _ZI3
        _now_et_hour = f"{datetime.now(_ZI3('America/New_York')).hour:02d}"
        _hour_blocked = _now_et_hour in getattr(CONFIG, "_day_hour_block", set())
        for sym in CONFIG.watchlist:
            if sym in held:
                continue
            if sym in getattr(CONFIG, "_day_symbol_cooldown", set()):
                continue
            if _hour_blocked:
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
            # Force re-evaluate after 5 min even if quant unchanged — macro/news evolve.
            key = (quant.direction, round(quant.strength / 0.05) * 0.05)
            cache_age = time.time() - self._eval_cache_ts.get(sym, 0.0)
            if self._eval_cache.get(sym) == key and cache_age < 300:
                continue
            self._eval_cache[sym] = key
            self._eval_cache_ts[sym] = time.time()
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

            # (b) Per-regime minimum confidence (stricter than global threshold).
            # Overnight: cap the regime floor at the looser overnight gate so the
            # regime playbook can't re-impose the strict RTH threshold at night —
            # EXCEPT in Crisis, whose 0.80 floor is a deliberate hard defense
            # (collapsing it to 0.50 overnight is exactly backwards: crisis vol
            # is worst in thin Globex hours).
            regime_min_conf = regime_params.get("min_conf", CONFIG.confidence_threshold)
            from zoneinfo import ZoneInfo as _ZIr
            _mr = datetime.now(_ZIr("America/New_York"))
            _minsr = _mr.hour * 60 + _mr.minute
            if not (9 * 60 + 30 <= _minsr <= 16 * 60) and str(rk).upper() != "CRISIS":
                regime_min_conf = min(regime_min_conf, CONFIG.confidence_threshold_overnight)
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
            # Day-adapt: apply EOD-learned side + regime size multipliers.
            _day_side_mult = getattr(CONFIG, "_day_side_size", {}).get(sig.side, 1.0)
            _day_regime_mult = getattr(CONFIG, "_day_regime_size", {}).get(rk, 1.0)
            _day_mult = _day_side_mult * _day_regime_mult
            if _day_mult != 1.0:
                size = round(size * _day_mult, 2)
            # Combined defensive multiplier for FUTURES contract sizing: the
            # USD `size` above never reaches futures_plan (which sizes off the
            # per-trade risk budget), so without this the circuit breaker,
            # regime haircut and day-adapt haircuts silently do nothing for
            # futures — every trade risks the full budget.
            risk_mult = (cb_mult * (w or 1.0)
                         * regime_params["size_mult"] * _day_mult)
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
                    from zoneinfo import ZoneInfo as _ZI2
                    _et2 = datetime.now(_ZI2("America/New_York"))
                    _m2 = _et2.hour * 60 + _et2.minute
                    _is_overnight = not (9 * 60 + 30 <= _m2 <= 16 * 60)
                    of_ok, of_reason = of.confirm_entry(sig.side, is_overnight=_is_overnight)
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
                plan = futures_plan(sig, sig.price, risk_mult=risk_mult)
                if plan is None:
                    notify(f"  skip (sizing: stop too wide / invalid ATR for risk budget): {sig.symbol}")
                    continue
                if self._topstep is not None:
                    eq_chk, _u = self._account_equity()
                    if (eq_chk - plan.risk_usd) <= self._topstep.mll_floor():
                        notify(f"  skip (worst-case loss ${plan.risk_usd:,.0f} would breach "
                               f"trailing MLL floor ${self._topstep.mll_floor():,.0f}): {sig.symbol}")
                        continue
                    # DLL analog: a full stop-out must also fit inside the
                    # remaining Daily-Loss-Limit headroom — otherwise the trade
                    # is allowed at day_pnl −$999, overshoots the $1k DLL, and
                    # only the after-the-fact flatten path catches it.
                    if CONFIG.topstep_responsible_trading:
                        day_pnl = eq_chk - self._topstep.day_start_equity
                        headroom = CONFIG.topstep_daily_loss_limit + day_pnl
                        if plan.risk_usd >= headroom:
                            notify(f"  skip (worst-case loss ${plan.risk_usd:,.0f} exceeds "
                                   f"DLL headroom ${max(headroom, 0):,.0f}): {sig.symbol}")
                            continue

            try:
                pos = self.executor.open(sig, size, self.state, risk_mult=risk_mult)
            except Exception as e:  # noqa: BLE001
                notify(f"  ! execute failed {sig.symbol}: {e}")
                continue
            if pos is None:
                notify(f"  skip (sizing rejected — no order placed): {sig.symbol}")
                continue
            qty = pos.qty
            # Fresh entry: drop any BE/trail state left by a prior trade in this
            # symbol that exited via flatten/reconcile (which don't pop it) — a
            # re-entry must not inherit be_done/partial_done/trail_peak.
            self._be_state.pop(sig.symbol, None)
            self.cooldowns[sig.symbol] = time.time()
            if not pos.shadow:
                self.state.journal_decision(
                    pos.symbol, pos.opened_at,
                    regime=rk,
                    quant_lean=sig.confidence,
                    qual_lean=sig.confidence,
                    confidence=sig.confidence,
                )
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
        # Futures roots (MNQ/ES/etc.) have no data on Alpaca's equities-only
        # /v2/stocks endpoint — route those through ProjectX's own history feed.
        # Fall back to Alpaca (self.data) for any non-futures symbol.
        broker = self.executor.broker
        if hasattr(broker, "historical_bars"):
            bars = broker.historical_bars(sym, timeframe=CONFIG.scalp_timeframe, limit=200)
        else:
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
        # (ES→SPX, NQ→NDX). Weight scales dynamically:
        #   base (config default, 30%) → aligned with quant (45%) → whale + aligned (60%)
        # "Aligned" = UW and quant lean point the same direction.
        if self._uw is not None:
            uw = self._uw.get(sym)
            if uw is not None:
                aligned = (uw.lean >= 0) == (quant.lean >= 0)
                if uw.whale and aligned:
                    w = min(0.60, CONFIG.uw_flow_lean_weight * 2.0)
                elif aligned:
                    w = min(0.45, CONFIG.uw_flow_lean_weight * 1.5)
                else:
                    w = CONFIG.uw_flow_lean_weight
                blended = (1.0 - w) * quant.lean + w * uw.lean
                blended = max(-1.0, min(1.0, blended))
                from signals import QuantRead
                whale_tag = " 🐋" if uw.whale else ""
                align_tag = f" w={w:.0%}" if w != CONFIG.uw_flow_lean_weight else ""
                quant = QuantRead(
                    lean=round(blended, 4),
                    strength=round(abs(blended), 4),
                    atr=quant.atr,
                    detail=quant.detail + f" · UW={uw.lean:+.2f}{whale_tag}{align_tag}",
                )

        if quant.direction == "FLAT":
            return None

        # ── Session phase: time-of-day confidence scaling ────────────────────
        # Research (Gao et al. JFE 2018): open 09:30-10:00 and close 15:30-16:00 ET
        # are momentum windows; midday and overnight have lower predictability.
        # Overnight Globex gets 50% size via confidence multiplier.
        from zoneinfo import ZoneInfo as _ZI
        _now_et = datetime.now(_ZI("America/New_York"))
        _h, _m = _now_et.hour, _now_et.minute
        _mins = _h * 60 + _m
        if (9 * 60 + 30) <= _mins <= (10 * 60):
            _phase, _phase_mult = "open_momentum", CONFIG.phase_mult_open
        elif (15 * 60 + 30) <= _mins <= (16 * 60):
            _phase, _phase_mult = "close_momentum", CONFIG.phase_mult_close
        elif (10 * 60) < _mins < (15 * 60 + 30):
            _phase, _phase_mult = "midday", CONFIG.phase_mult_midday
        else:
            _phase, _phase_mult = "overnight", CONFIG.phase_mult_overnight

        if _phase_mult < 1.0:
            from signals import QuantRead
            quant = QuantRead(
                lean=round(quant.lean * _phase_mult, 4),
                strength=round(quant.strength * _phase_mult, 4),
                atr=quant.atr,
                detail=quant.detail + f" · phase={_phase}({_phase_mult:.0%})",
            )

        # ── VWAP directional gate ─────────────────────────────────────────────
        # RTH VWAP (reset conceptually at 09:30 ET) used as intraday fair-value
        # anchor. Longs only above VWAP; shorts only below. Don't enter when price
        # is extended > 0.5×ATR from VWAP (chasing). Research: self-fulfilling
        # institutional benchmark; most execution desks target VWAP ± bands.
        _closes = bars.get("close") or []
        _volumes = bars.get("volume") or []
        if len(_closes) >= 20 and len(_volumes) >= 20 and sum(_volumes[-100:]) > 0:
            _n = min(len(_closes), len(_volumes))
            _tv = sum(_volumes[-_n:])
            _vwap = sum(_closes[i] * _volumes[i] for i in range(len(_closes) - _n, len(_closes))) / _tv if _tv > 0 else spot
            _vwap_dev = abs(spot - _vwap)
            _atr_val = quant.atr if quant.atr and quant.atr > 0 else spot * 0.001
            if _phase != "overnight":
                # VWAP gate: longs need price above VWAP, shorts need price below
                _vwap_ok = (spot > _vwap) if quant.direction == "BUY" else (spot < _vwap)
                # Extension filter: skip when price is already >0.5 ATR from VWAP
                _extended = _vwap_dev > 0.5 * _atr_val
                if not _vwap_ok:
                    return None  # direction conflicts with VWAP anchor
                if _extended:
                    return None  # chasing extended move; wait for pullback

        if quant.direction == "FLAT":
            return None

        # Classify regime for this symbol (used both by the gate and later by
        # the regime-adaptive playbook in run_once).
        regime_label = "Unknown"
        if CONFIG.regime_gate_enabled:
            _highs = bars.get("high") or bars["close"]
            _lows = bars.get("low") or bars["close"]
            if _phase == "overnight":
                # ATR-percentile regime is more robust overnight: SMA/trend signals
                # have near-zero edge during Globex sessions (arXiv 2605.04004).
                # EXTREME volatility = block trade; otherwise use SMA classifier.
                _atr_regime = classify_atr_percentile(bars["close"], _highs, _lows)
                if _atr_regime == "EXTREME":
                    return None  # overnight EXTREME vol — no new entries
                regime_label = classify_last(bars["close"], _highs, _lows)
            else:
                regime_label = classify_last(bars["close"], _highs, _lows)
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

        # Phase-aware confluence blend. Overnight quant is thin, so weight the LLM
        # stream more heavily and use the looser overnight gate; RTH stays 50/50 @
        # the strict threshold. Overnight = outside 09:30–16:00 ET.
        from zoneinfo import ZoneInfo as _ZIc
        _mc = datetime.now(_ZIc("America/New_York"))
        _mins_c = _mc.hour * 60 + _mc.minute
        _is_overnight_c = not (9 * 60 + 30 <= _mins_c <= 16 * 60)
        if _is_overnight_c:
            _qw = CONFIG.qual_weight_overnight
            _thresh = CONFIG.confidence_threshold_overnight
        else:
            _qw, _thresh = 0.5, CONFIG.confidence_threshold
        confidence = round((1.0 - _qw) * quant.strength + _qw * qual_conv, 3)
        if confidence < _thresh:
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

        Exception: when the local book has NO recorded trading history for this
        account (realized_pnl_usd == 0 — e.g. a fresh/blank local DB pointed at
        an account that already has real broker history), the conservative-min
        has nothing meaningful to reconcile against and would permanently clamp
        equity down to the raw account_size, tripping the trailing-MLL floor
        forever. In that case trust the live broker balance directly. On a failed
        read, fall back to the last trusted broker reading (not the raw
        account_size) so a transient connection error can't manufacture a false
        breach either.
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

        # Internal ledger view, anchored to the broker-synced baseline when one
        # has been captured. account_size + realized is only valid if the local
        # DB holds every trade since account inception — it usually doesn't, so
        # unanchored it clamps a profitable account down to a phantom figure the
        # moment the first local trade closes (false MLL breach).
        ledger_base = (self._equity_baseline if self._equity_baseline is not None
                       else CONFIG.topstep_account_size)
        internal_equity = ledger_base + self.state.realized_pnl_usd + unrealized
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
                if self._equity_baseline is None:
                    # First trusted read of this process: sync the ledger anchor.
                    self._equity_baseline = float(bal) - self.state.realized_pnl_usd
                    internal_equity = (self._equity_baseline
                                       + self.state.realized_pnl_usd + unrealized)
                # Real-time combined equity = realized cash balance + open MTM.
                broker_equity = float(bal) + unrealized
                self._last_good_broker_equity = broker_equity
                equity = min(internal_equity, broker_equity)
                peak_est = max(internal_equity, broker_equity)
                self._equity_trusted = True
        except Exception:  # noqa: BLE001
            pass
        if not self._equity_trusted and self._equity_baseline is None \
                and self._last_good_broker_equity is not None:
            # No broker sync yet + failed read: trust the last known-good broker
            # figure rather than falling all the way back to account_size.
            equity = self._last_good_broker_equity
            peak_est = max(peak_est, self._last_good_broker_equity)
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
            self._topstep.save_day_state(str(self._topstep_session_date()), True)
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
        # phantom: local open (non-shadow) with no matching broker position.
        # Matching is by symbol + side + qty, not symbol alone: a local BUY 2
        # vs broker BUY 1 (or SELL) is NOT the same position — symbol-only
        # matching let real side/qty drift persist unmanaged (seen live:
        # broker ES x2 vs adopted local ES x1).
        for pos in list(self.state.open_positions):
            if pos.shadow:
                continue
            bp = bro.get(pos.symbol.upper())
            b_side = str(bp.get("side", "")).upper() if bp else ""
            b_qty = float(bp.get("qty") or 0.0) if bp else 0.0
            if bp is not None and b_side == pos.side.upper() and b_qty == pos.qty:
                continue        # exact match — nothing to do
            if bp is not None and b_side == pos.side.upper() and 0 < b_qty != pos.qty:
                # Same direction, different size (partial fill / external
                # reduction): adopt the broker's qty as truth.
                notify(f"♻ reconcile: {pos.symbol} qty {pos.qty:g} → {b_qty:g} "
                       f"(broker truth adopted)")
                pos.qty = b_qty
                pos.size_usd = round(b_qty * pos.entry_price, 2)
                changed = True
                continue
            # No broker position, or side flipped: the local position is gone.
            mark = self._mark(pos.symbol)
            exit_price = mark if mark is not None else pos.entry_price
            self.state.close(pos, exit_price)
            if self._topstep is not None:
                self._topstep.record_close(pos.pnl_usd, hold_seconds(pos.opened_at))
            notify(f"♻ reconcile: phantom {pos.symbol} not at broker "
                   f"({'side mismatch' if bp is not None else 'flat'}) — closed "
                   f"locally @ ~{exit_price:.2f} pnl=${pos.pnl_usd:.2f}")
            changed = True

        # orphan: broker position with no local counterpart. In-watchlist →
        # adopt and manage. OFF-watchlist → flatten immediately: this bot never
        # opens such positions, so they come from another writer on the account
        # (rogue process / stale cloud deploy / manual) and carrying them
        # unmanaged with no stop is pure unpriced risk (seen live: repeated
        # full-size ES 1-lots appearing on a MES/MNQ-only account).
        local_syms = {p.symbol.upper() for p in self.state.open_positions if not p.shadow}
        watch = {w.upper() for w in CONFIG.watchlist}
        for sym, bp in bro.items():
            if sym in local_syms:
                continue
            qty = float(bp.get("qty") or 0.0)
            if qty <= 0:
                continue
            side = bp.get("side", "BUY")
            if sym not in watch:
                cid = bp.get("contract_id")
                try:
                    if cid:
                        self.executor.broker._post(  # noqa: SLF001
                            "/api/Position/closeContract",
                            {"accountId": self.executor.broker.account_id,
                             "contractId": cid})
                    notify(f"🛑 reconcile: FOREIGN position {sym} {side} qty={qty} "
                           f"(not in watchlist) — FLATTENED. Another process is "
                           f"trading this account; rotate the ProjectX API key.")
                except Exception as exc:  # noqa: BLE001
                    notify(f"⚠ reconcile: failed to flatten foreign {sym}: {exc} "
                           f"— adopting for EOD flatten instead")
                    entry = float(bp.get("avg_price") or 0.0) or (self._mark(sym) or 0.0)
                    self.state.add(Position(
                        symbol=sym, asset="future", side=side, qty=qty,
                        entry_price=entry, size_usd=entry * qty, stop=0.0, target=0.0,
                        kind="adopted", thesis="foreign orphan (flatten failed)",
                        opened_at=datetime.now(timezone.utc).isoformat(),
                        mode=self.executor.mode, order_id="", filled=True,
                    ))
                changed = True
                continue
            entry = float(bp.get("avg_price") or 0.0) or (self._mark(sym) or 0.0)
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
            # Cancel resting protective stops BEFORE closing: a stop left
            # working after the flatten would fill on the next trade-through
            # and open a brand-new naked position on a halted account.
            for pos in open_futures:
                pid = getattr(pos, "protective_order_id", "")
                if pid:
                    try:
                        self.executor.broker.cancel_order(pid)
                        pos.protective_order_id = ""
                    except Exception as e:  # noqa: BLE001
                        notify(f"  ! stop cancel failed {pos.symbol} ({pid}): {e}")
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

    # ── partial exits + BE ratchet + ATR trail ────────────
    def _manage_partial_exit(self, pos: "Position", mark: float) -> None:
        """Three-step partial-exit ladder for futures positions (research: NQ backtest
        ATR-trail PF 1.6 vs fixed-tick PF 1.1).

        Step 1 — BE ratchet at +1R: move stop to entry_price.
        Step 2 — Partial at +1.5R: close floor(qty/2), remaining qty runs to target.
        Step 3 — Chandelier trail: after BE+partial, trail peak minus 1.5×ATR(implied).

        State stored in self._be_state[pos.symbol] so it survives scan ticks.
        Shadow positions are skipped (no broker order, pure bookkeeping).
        Only runs when pos.stop is set (adopted orphans with stop=0 are skipped).
        """
        if pos.shadow or pos.qty < 1 or not pos.stop or pos.stop == 0.0:
            return
        long = pos.side == "BUY"
        initial_risk = abs(pos.entry_price - pos.stop)
        if initial_risk <= 0:
            return

        st = self._be_state.setdefault(pos.symbol, {
            "partial_done": False, "be_done": False,
            "trail_peak": mark,
        })
        if long:
            st["trail_peak"] = max(st["trail_peak"], mark)
        else:
            st["trail_peak"] = min(st["trail_peak"], mark)

        pnl_r = ((mark - pos.entry_price) * (1.0 if long else -1.0)) / initial_risk

        # Step 1: move stop to BE at +1R
        if not st["be_done"] and pnl_r >= 1.0:
            old_stop = pos.stop
            pos.stop = pos.entry_price
            st["be_done"] = True
            self._amend_native_stop(pos, st)
            notify(f"  BE-RATCHET {pos.symbol}: stop {old_stop:.2f} → {pos.entry_price:.2f} "
                   f"(+{pnl_r:.1f}R breakeven locked)")

        # Step 2: partial close at +1.5R
        if not st["partial_done"] and pnl_r >= 1.5 and pos.qty >= 2:
            close_qty = max(1, pos.qty // 2)
            if close_qty < pos.qty:
                ok = self.executor.close_partial(pos, close_qty, mark)
                if ok:
                    partial_pnl = (
                        (mark - pos.entry_price)
                        * (1.0 if long else -1.0)
                        * close_qty
                        * dollar_value_per_point(pos.symbol)
                    )
                    pos.qty -= close_qty
                    pos.size_usd = round(pos.qty * pos.entry_price, 2)
                    st["partial_done"] = True
                    # Book the realized partial into the ledger — state.close()
                    # only books full closes, so without this internal equity
                    # permanently understates profit (false-MLL-breach fuel).
                    self.state.realized_pnl_usd += partial_pnl
                    # Re-arm the resting stop for the remaining contracts:
                    # close_partial cancels the original full-qty stop and the
                    # remainder must not run naked against the trailing MLL.
                    self.executor.replace_protective_stop(pos, pos.stop)
                    if pos.protective_order_id:
                        st["armed_stop"] = pos.stop
                    if self._topstep is not None and not pos.shadow:
                        from topstep_risk import hold_seconds
                        self._topstep.record_close(partial_pnl, hold_seconds(pos.opened_at))
                    notify(f"  PARTIAL-EXIT {pos.symbol}: closed {close_qty} @ {mark:.2f} "
                           f"(+{pnl_r:.1f}R), {pos.qty} remaining, trailing ATR")

        # Step 3: Chandelier trail after BE + partial done.
        # ATR_implied ≈ initial_risk / ATR_STOP_MULT (stop was placed at mult×ATR).
        # Trail at 1.5×ATR_implied anchored to peak since entry.
        if st["be_done"] and st["partial_done"]:
            atr_implied = initial_risk / max(CONFIG.atr_stop_mult, 1.0)
            trail_dist = 1.5 * atr_implied
            if long:
                trail_stop = st["trail_peak"] - trail_dist
                if trail_stop > pos.stop:
                    pos.stop = trail_stop
            else:
                trail_stop = st["trail_peak"] + trail_dist
                if trail_stop < pos.stop:
                    pos.stop = trail_stop
            # Push the trail to the exchange, throttled to ≥25% of the trail
            # distance per amend so a trending tick stream doesn't spam
            # cancel/replace every scan.
            self._amend_native_stop(pos, st, min_move=0.25 * trail_dist)

    def _amend_native_stop(self, pos: "Position", st: dict,
                           min_move: float = 0.0) -> None:
        """Move the exchange-resting protective stop up to the ratcheted
        pos.stop. Client-side ratchets alone are worthless in a disconnect —
        the broker keeps honoring the original wide stop. min_move throttles
        chandelier-driven cancel/replace churn."""
        if pos.shadow or not pos.stop or not math.isfinite(pos.stop):
            return
        armed = st.get("armed_stop")
        if armed is not None and abs(pos.stop - armed) < max(min_move, 1e-9):
            return
        pid = getattr(pos, "protective_order_id", "")
        cancel = getattr(self.executor.broker, "cancel_order", None)
        if pid and callable(cancel):
            try:
                cancel(pid)
            except Exception as exc:  # noqa: BLE001
                notify(f"  ⚠ cancel stop {pid} for amend failed ({pos.symbol}): {exc}")
            pos.protective_order_id = ""
        self.executor.replace_protective_stop(pos, pos.stop)
        if pos.protective_order_id:
            st["armed_stop"] = pos.stop

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
            self._manage_partial_exit(pos, mark)
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
            self._be_state.pop(pos.symbol, None)
            tag = "EXIT-SHADOW" if pos.shadow else "CLOSE"
            exit_px = pos.exit_price if pos.exit_price is not None else mark
            notify(f"{tag} {reason} {pos.symbol} @ {exit_px:.2f} pnl=${pos.pnl_usd:.2f}")
            if CONFIG.manual_tickets and not pos.shadow:
                notify(exit_ticket(pos, exit_px, reason))
