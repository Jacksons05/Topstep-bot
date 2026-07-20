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
import threading
import time
from datetime import date, datetime, timezone
import day_learner as _day_learner
import singleton_lock

from bar_clock import parse_timeframe_seconds, BarClock

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
from futures_symbols import (
    contracts_for_mini_budget,
    dollar_value_per_point,
    is_futures_symbol,
    mini_equivalents,
)
from flow_risk import FlowRiskManager, FlowRead
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
        # Refuse to start a second engine process against the same account/
        # state — must happen before anything else touches the broker or DB.
        self._lock_path = singleton_lock.acquire()
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
        # Event-driven wake-up (see bar_clock.py / wait_for_next_cycle): set by
        # a BarClock the instant a live tick reveals a bar has closed. Created
        # unconditionally — with no feed attached nothing ever sets it, so
        # wait_for_next_cycle's timeout alone reproduces today's pure-polling
        # behavior exactly.
        self.wake_event = threading.Event()
        # Flow-risk overlays (flow_risk.py): A10 vol-target sizing (folded into
        # the futures risk multiplier, de-risk only) + A8 toxicity veto. Reads
        # are computed from the bars already fetched in _prescreen (no extra
        # network) and stashed per symbol for the entry loop.
        self._flow: FlowRiskManager | None = (
            FlowRiskManager()
            if (CONFIG.vol_sizing_enabled or CONFIG.toxicity_veto_enabled)
            else None
        )
        self._flow_reads: dict[str, FlowRead] = {}
        self._uw = None     # UWFlowFeed when UW_FLOW_ENABLED=true + UW_API_KEY set
        self._gex = None    # UWGexFeed when ENTRY_ENGINE=gex + UW_API_KEY set
        self._gex_regimes: dict[str, str] = {}   # symbol → regime at prescreen time
        self._account_key: str = ""              # broker account id (account_history key)
        self._last_equity_record: float = 0.0    # throttle for state.record_equity
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
        # Symbols with an unresolved AMBIGUOUS exit (a transport failure after
        # the order may have already reached the exchange — H-CRIT-2). Exit
        # management for these is held until the next successful
        # _reconcile_positions() pass resolves broker truth; blindly retrying
        # could double-execute or flip the position onto the opposite side.
        self._exit_ambiguous: set[str] = set()
        if CONFIG.topstep_mode_enabled and CONFIG.projectx_username and CONFIG.projectx_api_key:
            live_broker = None
            try:
                from projectx_executor import ProjectXBroker
                live_broker = ProjectXBroker()
            except Exception as e:  # noqa: BLE001
                notify(
                    f"⚠ ProjectX connection failed ({e}) — running the agentic "
                    "pipeline on the base executor (Sim/paper). Check PROJECTX "
                    "credentials / network and restart to retry."
                )
                live_broker = None

            if live_broker is not None:
                self._attach_topstep_broker(live_broker)

                # Order-flow feed + protective-stop revalidation only make
                # sense once the live broker + Topstep risk layer are BOTH
                # attached together (guarded above).
                if self._topstep is not None:
                    # Live order-flow feed: stream quotes + trades + depth off
                    # the ProjectX market hub into per-symbol OBI/CVD/whale
                    # engines. Only the futures roots in the watchlist get
                    # subscribed.
                    if CONFIG.orderflow_gate_enabled:
                        try:
                            from projectx_marketdata import ProjectXOrderFlowFeed
                            self._oflow = ProjectXOrderFlowFeed(self.executor.broker)
                            n = self._oflow.subscribe(list(CONFIG.watchlist))
                            notify(f"📡 Order-flow feed: subscribed {n} futures root(s) "
                                   f"(OBI/CVD/whale gate {'live' if n else 'idle — no futures roots'})")
                            # Wire the bar-close wake-up: only meaningful once
                            # ticks are actually flowing (n>0, live/non-mock
                            # connection) — otherwise nothing would ever pulse
                            # it and it'd be dead weight.
                            if n > 0 and CONFIG.event_driven_loop_enabled:
                                period = parse_timeframe_seconds(CONFIG.scalp_timeframe)
                                self._oflow.attach_bar_clock(
                                    BarClock(period, self.wake_event.set)
                                )
                                notify(f"⚡ event-driven wake-ups enabled "
                                       f"(bar={CONFIG.scalp_timeframe} / {period}s — "
                                       "run_once fires on the first tick after a bar "
                                       "close, capped by the existing poll interval)")
                        except Exception as e:  # noqa: BLE001
                            notify(f"⚠ Order-flow feed init failed ({e}) — gate disabled (fails open)")
                            self._oflow = None
                    # A restart may have orphaned the native protective stops
                    # the previous process placed — re-arm them
                    # deterministically. Isolated in its own try/except: a
                    # failure here must not discard the already-successfully-
                    # constructed Topstep risk layer above.
                    try:
                        self._revalidate_protective_stops()
                    except Exception as e:  # noqa: BLE001
                        notify(f"⚠ protective-stop revalidation failed at startup ({e})")
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

        # GEX-regime entry engine (Phase 4). Init failure does NOT fall back to
        # the legacy SMA/RSI signal (negative EV, Rounds 1/19) — with no feed
        # every symbol reads neutral and entries stay locked (fail closed).
        if CONFIG.entry_engine == "gex" and CONFIG.uw_api_key:
            try:
                from uw_gex import UWGexFeed
                self._gex = UWGexFeed()
                notify("🎛 GEX-regime entry engine enabled "
                       f"(neutral_band={CONFIG.gex_neutral_band_frac:.0%} of median |GEX| | "
                       f"MR_dev={CONFIG.gex_mr_atr_dev:g} ATR | "
                       f"breakout_lookback={CONFIG.gex_breakout_lookback} | "
                       f"neg_risk_mult={CONFIG.gex_neg_risk_mult:g})")
            except Exception as e:  # noqa: BLE001
                notify(f"⚠ GEX feed init failed ({e}) — entries will stay LOCKED "
                       "(neutral fail-closed); fix the feed or set ENTRY_ENGINE=legacy")
                self._gex = None

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
        """True during CME futures trading hours.

        CME Globex session: Sun 18:00 ET → Fri 17:00 ET, with a daily
        maintenance break 17:00–18:00 ET. Saturday is always closed.
        Always True when MARKET_HOURS_ONLY is off.
        """
        if not CONFIG.market_hours_only:
            return True
        from zoneinfo import ZoneInfo
        import cme_calendar
        now = datetime.now(ZoneInfo("America/New_York"))
        wd = now.weekday()   # Mon=0 … Sun=6
        mins = now.hour * 60 + now.minute
        # Saturday: always closed
        if wd == 5:
            return False
        # Sunday: open only after 18:00 ET (Globex Sunday open)
        if wd == 6:
            return mins >= 18 * 60
        # Friday: weekend starts at the 17:00 ET close (no 18:00 reopen)
        if wd == 4 and mins >= 17 * 60:
            return False
        # Exchange holidays: full closures have no session at all; early-close
        # days halt at cme_calendar's (conservative) time until the 18:00 reopen.
        if cme_calendar.is_full_closure(now.date()):
            return False
        halt = cme_calendar.early_close_halt(now.date())
        if halt is not None:
            halt_mins = halt.hour * 60 + halt.minute
            if halt_mins <= mins < 18 * 60:
                return False
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

    def _attach_topstep_broker(self, live_broker) -> None:
        """Attach a live, already-connected ProjectX broker together with a
        freshly-constructed TopstepRiskManager, atomically (H-CRIT-1).

        A live, connected ProjectX broker must NEVER end up assigned to
        self.executor.broker while self._topstep stays None — that
        combination trades a real account with zero Topstep-specific
        protection (MLL, DLL, contract cap, flatten, econ blackout) and
        nothing else in the engine loop would ever notice or warn about it
        again. So: self.executor.broker is only ever reassigned to
        `live_broker` in the success path below. If anything raises before
        that line, self.executor.broker is simply never touched — it stays
        whatever build_executor() set (the safe base/Sim executor), and
        self._topstep stays None.

        Extracted from __init__ specifically so this atomicity can be unit
        tested without constructing a full Engine (which would need real
        market-data/news/LLM clients) — see tests/test_live_readiness.py.
        """
        try:
            from topstep_risk import TopstepRiskManager
            # Seed the trailing-MLL peak / day base from the live balance.
            # Falls back to the account size on failure.
            try:
                acct = live_broker.account()
                initial_equity = acct.get("equity") or CONFIG.topstep_account_size
            except Exception:  # noqa: BLE001
                initial_equity = CONFIG.topstep_account_size
            # Bug #7 fix: reseed the peak from the DB's historical maximum for
            # THIS broker account, not the (possibly drawn-down) current
            # balance. A DB error here deliberately propagates to the outer
            # except — better to stay on the safe Sim executor than to trade
            # live behind a floor computed off an understated peak. None (no
            # rows) is the one legitimate "fresh cycle" case where the
            # balance-based seed inside TopstepRiskManager stands.
            self._account_key = str(getattr(live_broker, "account_id", "") or "")
            hist_peak = None
            from state import DATABASE_URL as _DB_URL, historical_peak_equity
            if _DB_URL and self._account_key:
                hist_peak = historical_peak_equity(self._account_key)
            topstep = TopstepRiskManager(initial_equity=initial_equity,
                                         historical_peak=hist_peak)
            # Restore the persisted session anchors (DLL day base, MLL peak,
            # day-halt) so a restart can't re-arm a fresh daily allowance.
            _sess = str(self._topstep_session_date())
            _restored, _halt = topstep.load_day_state(_sess)
            if topstep.cold_start_unsafe():
                # Persisted state was expected but missing/corrupt — the MLL peak
                # may be understated. preflight FAILs on this so run.py won't even
                # start; belt-and-suspenders halt entries if we somehow got here.
                self._topstep_day_halt = True
                notify("🛑 Topstep: persisted risk state corrupt/missing at cold "
                       "start — entries HALTED (fail closed). Restore or remove "
                       "topstep_day_state.json.")
            if _restored:
                self._topstep_last_reset = self._topstep_session_date()
                if _halt:
                    self._topstep_day_halt = True
                    notify("🛑 Topstep: DAY HALT restored from persisted "
                           "session state — no new entries until next session")
            # Attach broker + risk layer together, atomically, only once
            # both are known-good.
            self.executor.broker = live_broker
            self._topstep = topstep
            notify(
                "⚡ TOPSTEP/PROJECTX MODE ACTIVE — "
                f"env={'live' if CONFIG.projectx_live else 'sim'} | "
                f"account=${CONFIG.topstep_account_size:,.0f} | "
                f"trailing_MLL=${CONFIG.topstep_trailing_mll:,.0f} | "
                f"daily_loss=${CONFIG.topstep_daily_loss_limit:,.0f} | "
                f"max_contracts={CONFIG.topstep_max_contracts} | "
                f"flatten_at={CONFIG.topstep_flatten_time} ET"
            )
        except Exception as e:  # noqa: BLE001
            notify(
                f"🛑 TOPSTEP RISK LAYER init failed ({e}) after a "
                "successful ProjectX connection — REFUSING to attach "
                "the live broker without Topstep protection. Staying "
                "on the base (Sim) executor. Restart to retry."
            )
            self._topstep = None

    def close(self) -> None:
        self.data.close()
        self.news.close()
        self.macro.close()
        self.events.close()
        self.executor.close_broker()
        self.state.save()
        if self._uw is not None:
            self._uw.close()
        if self._gex is not None:
            self._gex.close()
        if self._uw_log is not None:
            self._uw_log.close()
        singleton_lock.release(self._lock_path)

    # ── adaptive cadence: poll faster when the breaker trips, idle when closed ──
    def next_interval(self) -> int:
        if not self._market_open():
            return CONFIG.closed_interval_sec
        return CONFIG.fast_interval_sec if self.cb_level != "green" else CONFIG.scan_interval_sec

    def wait_for_next_cycle(self) -> None:
        """Block until the next run_once() should fire.

        Replaces a plain `time.sleep(next_interval())` with a wait on
        `wake_event`, which a live order-flow feed's BarClock sets the
        instant a tick reveals a bar has closed (see bar_clock.py). The
        `next_interval()` value is still passed as the wait's timeout, so
        this can only ever fire EARLIER than the old pure-polling loop, never
        later — a dead feed, mock mode, or EVENT_DRIVEN_LOOP_ENABLED=false
        reproduces the exact old behavior via the timeout alone.

        Deliberately does not change what run_once() does or how often it's
        *allowed* to run — only how promptly a real bar close is noticed.
        run_once()'s own safety-critical sequencing (DB heartbeat, kill
        switch, reconciliation, MLL/DLL) is untouched and still runs exactly
        once per wake-up, same as every scheduled tick today.
        """
        timeout = self.next_interval()
        self.wake_event.wait(timeout=timeout)
        self.wake_event.clear()

    # ── fail-closed DB guard (Phase 2) ────────────────────
    def _db_panic_flatten_and_exit(self, why: str) -> None:
        """The state database died MID-SESSION. Trading on without it is the
        2026-07-14 blow-up: in-memory state can't persist, the next restart
        loads an empty/old book and re-opens duplicate positions. So: panic
        flatten through the broker (bounded retries — the exchange-resting
        protective stops still cover anything a failed flatten leaves), then
        hard-exit(1). systemd restarts us, preflight's State-backend check
        FAILs while the DB is down, and the Requires=postgresql.service
        dependency keeps the bot from even starting until Postgres is back.
        SystemExit deliberately bypasses run.py's per-cycle `except Exception`.
        """
        import sys as _sys
        notify(f"🚨 CRITICAL: state DB unreachable mid-session ({why}) — "
               "PANIC FLATTEN + hard exit (refusing to trade stateless)")
        broker = getattr(self.executor, "broker", None)
        flatten = getattr(broker, "flatten_all", None)
        if callable(flatten):
            for attempt in range(1, 4):
                try:
                    flatten()
                    notify("🚨 panic flatten: broker confirms flat/closing")
                    break
                except Exception as exc:  # noqa: BLE001
                    notify(f"🚨 panic flatten attempt {attempt}/3 failed: {exc}"
                           + ("" if attempt < 3 else
                              " — EXITING ANYWAY; exchange-resting stops are the "
                              "remaining protection. MANUAL CHECK REQUIRED."))
                    time.sleep(2.0)
        _sys.exit(1)

    def _db_heartbeat(self) -> None:
        """Live DB connectivity check on EVERY engine cycle — the per-tick
        analog of preflight's startup State-backend gate. Stateless mode
        (DATABASE_URL unset) is a deliberate dev configuration and passes."""
        from state import DATABASE_URL as _DB_URL, ping as _ping
        if not _DB_URL:
            return
        ok, why = _ping()
        if not ok:
            self._db_panic_flatten_and_exit(why)

    # ── one full cycle ────────────────────────────────────
    def run_once(self) -> None:
        # Fail-closed DB guard FIRST: nothing in this cycle — reconcile,
        # risk checks, entries, exits — may run against a dead state backend.
        self._db_heartbeat()

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

        # Market open: self-heal a silently-dead order-flow connection (dropped
        # subscriptions after an automatic reconnect, or an expired hub token).
        if self._oflow is not None:
            try:
                self._oflow.heal_if_stale()
            except Exception as _e:  # noqa: BLE001
                notify(f"⚠ order-flow self-heal failed: {_e}")

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

        # Naked-position guard (2026-07-17 incident): every cycle, no open real
        # futures position may rest without a CONFIRMED native protective stop.
        # Re-arm a missing stop; if that fails, flatten (broker-confirmed, retried
        # next cycle on failure — never booked closed on an unconfirmed result).
        # The one-shot flatten in executor.open() is not enough: during the MCL
        # incident a broker outage made that flatten a no-op and nothing retried.
        self._enforce_stops_or_flatten()

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
                # Bug #7: append the equity observation to Postgres
                # account_history (throttled ~1/min) so a cold boot reseeds
                # the trailing-MLL peak from the TRUE historical maximum even
                # if the local JSON day-state is lost. A failed write is a
                # dead state backend → same panic path as the heartbeat.
                if self._account_key and time.time() - self._last_equity_record >= 60.0:
                    try:
                        from state import record_equity
                        record_equity(self._account_key, self._peak_equity_est)
                        self._last_equity_record = time.time()
                    except Exception as exc:  # noqa: BLE001
                        self._db_panic_flatten_and_exit(
                            f"account_history write failed: {exc}")
                breached, why = self._topstep.risk_breach(equity, self.state, unrealized)
                if breached:
                    # Latch the ENTRY halt once (persist it), but do NOT use that
                    # latch to gate the flatten: re-flatten every scan until the
                    # broker confirms flat. _topstep_flatten_all only books
                    # positions the broker confirms closed, so a failed first
                    # attempt leaves them open and this retries instead of
                    # riding a naked position all day.
                    if not self._topstep_day_halt:
                        notify(f"🛑 TOPSTEP BREACH — {why} | flattening all & halting new entries")
                        self._topstep_day_halt = True
                        self._topstep.save_day_state(str(today), True)
                    if any(not p.shadow for p in self.state.open_positions):
                        self._topstep_flatten_all(why)
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

            # (A8) Flow-toxicity veto: stand aside when BVC-VPIN sits in the top
            # tail of this symbol's own toxicity history. Risk filter only (see
            # flow_risk.py). The same read supplies the (A10) vol multiplier below.
            flow = self._flow_reads.get(sig.symbol)
            if flow is not None and flow.veto:
                notify(f"  skip (toxicity: {flow.veto_reason}): {sig.symbol}")
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
            # (A10) vol-target multiplier folded in. futures_plan caps risk_mult
            # at 1.0, so on the funded account this can only DE-RISK in an
            # elevated-vol regime, never size up.
            _vol_mult = min(1.0, flow.vol_mult) if flow is not None else 1.0
            # GEX pivot: negative-gamma (vol-expanded) breakout entries run
            # strictly micro-tier — halve the risk budget on top of the other
            # defensive multipliers (futures_plan caps the product at 1.0).
            _gex_mult = (CONFIG.gex_neg_risk_mult
                         if self._gex_regimes.get(sig.symbol) == "negative" else 1.0)
            # Eval-pass sizing: de-risk after a loss streak and taper toward the
            # MLL floor (edge-independent variance control). Only ever ≤ 1.0.
            _eval_mult = 1.0
            if self._topstep is not None:
                _eq_for_size, _ = self._account_equity()
                _eval_mult = self._topstep.combined_size_mult(_eq_for_size)
            risk_mult = (cb_mult * (w or 1.0)
                         * regime_params["size_mult"] * _day_mult * _vol_mult
                         * _gex_mult * _eval_mult)
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
            qty_cap: int | None = None
            if is_futures_symbol(sig.symbol):
                # Cap the new order to the remaining ACCOUNT-WIDE contract capacity
                # so a single (esp. micro) trade can't size to the full limit and
                # push the account total over TOPSTEP_MAX_CONTRACTS. Only enforced
                # when the Topstep layer is active (the cap is a Topstep rule).
                if self._topstep is not None:
                    # Account-wide cap is in MINI-equivalents (5 minis = 50
                    # micros @ 10:1). Convert the open book to mini-equivalents,
                    # then convert the remaining budget back into whole contracts
                    # OF THIS symbol — so a micro can size up to ratio× the
                    # remaining minis and both sites speak the same unit as
                    # topstep_risk.contracts_ok().
                    ratio = CONFIG.topstep_micro_ratio
                    open_mini = sum(mini_equivalents(p.symbol, int(p.qty), ratio)
                                    for p in self.state.open_positions
                                    if not p.shadow)
                    remaining_mini = CONFIG.topstep_max_contracts - open_mini
                    qty_cap = contracts_for_mini_budget(sig.symbol, remaining_mini, ratio)
                    if qty_cap < 1:
                        notify(f"  skip (Topstep account-wide contract cap "
                               f"{CONFIG.topstep_max_contracts} mini-equiv reached — "
                               f"{open_mini:.1f} open): {sig.symbol}")
                        continue
                plan = futures_plan(sig, sig.price, risk_mult=risk_mult,
                                    max_contracts=qty_cap,
                                    atr_mult=regime_params.get("atr_stop_mult"))
                if plan is None:
                    notify(f"  skip (sizing: stop too wide / invalid ATR / no contract "
                           f"capacity for risk budget): {sig.symbol}")
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
                pos = self.executor.open(sig, size, self.state, risk_mult=risk_mult,
                                         max_contracts=qty_cap,
                                         atr_mult=regime_params.get("atr_stop_mult"))
            except Exception as e:  # noqa: BLE001
                if e.__class__.__name__ == "OrderStateUnknown":
                    notify(f"  ⚠ {sig.symbol} order state UNKNOWN (transport error "
                           f"after send) — NOT resubmitted; if it filled, next-cycle "
                           f"reconcile adopts the broker position")
                else:
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

    def _session_vwap(self, bars: dict) -> float | None:
        """RTH-anchored session VWAP (extracted from the legacy inline gate so
        the GEX mean-reversion strategy shares the identical anchoring logic).

        Anchors to today's 09:30 ET open when the bars carry per-bar timestamps
        (ProjectX futures feed) — without the reset this becomes a multi-session
        rolling VWAP that lags far behind on any trending day. Falls back to the
        full min-length window when timestamps are absent. None when volume is
        missing/zero (callers must treat that as 'no VWAP available')."""
        closes = bars.get("close") or []
        volumes = bars.get("volume") or []
        if len(closes) < 20 or len(volumes) < 20 or sum(volumes[-100:]) <= 0:
            return None
        from zoneinfo import ZoneInfo as _ZIv
        now_et = datetime.now(_ZIv("America/New_York"))
        n = min(len(closes), len(volumes))
        times = bars.get("time") or []
        start = len(closes) - n  # default: full min-length window
        if len(times) == len(closes):
            rth_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
            for i in range(len(times)):
                try:
                    bt = datetime.fromisoformat(times[i]).astimezone(_ZIv("America/New_York"))
                except (ValueError, TypeError):
                    continue
                if bt >= rth_open:
                    start = i
                    break
        seg_c = closes[start:]
        seg_v = volumes[start:]
        tv = sum(seg_v)
        if tv <= 0:
            return None
        return sum(seg_c[i] * seg_v[i] for i in range(len(seg_c))) / tv

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
        # ── Signal source ─────────────────────────────────────────────────
        if CONFIG.entry_engine == "off":
            # Round 21 verdict (HYPOTHESES.md): no candidate entry strategy has
            # survived OOS testing — the honest default is NO new entries.
            # Open positions are still managed/flattened by everything above.
            return None
        if CONFIG.entry_engine == "gex":
            # Phase 4 pivot: the dealer net-GEX regime replaces SMA/RSI (and
            # the optional ML read) as the entry idea. positive → VWAP
            # mean-reversion, negative → reduced-risk breakout, neutral (or a
            # dead/missing GEX feed) → no entries, fail closed.
            regime = self._gex.get(sym).regime if self._gex is not None else "neutral"
            self._gex_regimes[sym] = regime
            from gex_strategy import gex_quant_signal
            quant = gex_quant_signal(bars, regime, self._session_vwap(bars))
        else:
            # Legacy path: ML quant signal first (LightGBM on bar + order-flow
            # features); fall back to the deterministic indicator model when no
            # model is loaded or it has no opinion. Same QuantRead either way.
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
                raw_quant_lean = quant.lean  # pre-blend, for the signal logger
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
                if self._uw_log is not None:
                    self._uw_log.log(sym, spot, uw.lean, raw_quant_lean)

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

        # ── VWAP directional gate (legacy engine only) ────────────────────────
        # RTH VWAP (reset at 09:30 ET) used as intraday fair-value anchor. Longs
        # only above VWAP; shorts only below. Don't enter when price is extended
        # > 0.5×ATR from VWAP (chasing). Research: self-fulfilling institutional
        # benchmark; most execution desks target VWAP ± bands.
        # BYPASSED when ENTRY_ENGINE=gex: the positive-gamma mean-reversion leg
        # deliberately buys BELOW / sells ABOVE VWAP at ≥1 ATR extension — the
        # exact opposite of this momentum gate (gex_strategy.py owns its own
        # VWAP logic via _session_vwap).
        if CONFIG.entry_engine != "gex":
            _vwap = self._session_vwap(bars)
            if _vwap is not None and _phase != "overnight":
                _vwap_dev = abs(spot - _vwap)
                _atr_val = quant.atr if quant.atr and quant.atr > 0 else spot * 0.001
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

        # Flow-risk read (A10 vol-target sizing + A8 toxicity veto) from the bars
        # already in hand -- no extra fetch. Consumed in the entry loop.
        if self._flow is not None:
            self._flow_reads[sym] = self._flow.assess(bars)

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
        # Never fall back to the Alpaca equities quote for a futures root: it
        # resolves the root to a same-named stock (ES=Eversource ~$60) and the
        # resulting phantom mark can trip a spurious trailing-MLL breach ->
        # wrongful _topstep_flatten_all. A futures mark must come only from the
        # ProjectX flow feed / BBO above; if that is unavailable, return None
        # and let callers skip the mark (they must not assume 0).
        if is_futures_symbol(symbol):
            return None
        q = self.data.quote(symbol)
        return q.price if q and q.price > 0 else None

    def _live_projectx(self) -> bool:
        """True only when executing against a real (non-mock) ProjectX account."""
        b = self.executor.broker
        return b.__class__.__name__ == "ProjectXBroker" and not getattr(b, "_mock_mode", True)

    def _enforce_stops_or_flatten(self) -> None:
        """Per-cycle naked-position guard. Any open real futures position lacking
        a confirmed native protective stop is first re-armed; if the stop can't
        be placed, the position is FLATTENED (broker-confirmed). On any broker
        error the action RAISES/logs and is retried next cycle — a position is
        NEVER booked closed on an unconfirmed result, and never left silently
        naked. Complements executor._flatten_unprotected (one-shot at entry)
        with a persistent retry the entry path can't provide."""
        if not self._live_projectx():
            return
        broker = self.executor.broker
        for pos in list(self.state.open_positions):
            if pos.shadow or not is_futures_symbol(pos.symbol) or not pos.filled:
                continue
            if getattr(pos, "protective_order_id", ""):
                continue  # already protected by a native resting stop
            # Unprotected. Try to re-arm the native stop first.
            armed = False
            if pos.stop and pos.stop > 0:
                stop_side = "SELL" if pos.side == "BUY" else "BUY"
                try:
                    try:
                        oid = broker.place_stop_order(pos.symbol, int(pos.qty),
                                                      stop_side, pos.stop,
                                                      mark=self._mark(pos.symbol))
                    except TypeError:
                        oid = broker.place_stop_order(pos.symbol, int(pos.qty),
                                                      stop_side, pos.stop)
                    if oid:
                        pos.protective_order_id = oid
                        armed = True
                        notify(f"🛡 {pos.symbol} was unprotected → native stop "
                               f"re-armed {stop_side} @ {pos.stop:.2f}")
                except Exception as e:  # noqa: BLE001
                    notify(f"⚠ {pos.symbol} stop re-arm failed ({e}) — flattening")
            if armed:
                continue
            # Still naked → flatten. flatten_all is broker-confirmed and RAISES
            # on an outage (fail-closed), so a failure just retries next cycle;
            # reconcile books the close once the broker confirms flat.
            try:
                broker.flatten_all()
                notify(f"🛑 {pos.symbol} could not be protected — FLATTENED "
                       f"(naked-position guard)")
            except Exception as e:  # noqa: BLE001
                notify(f"🚨 {pos.symbol} NAKED and flatten FAILED ({e}) — broker "
                       f"may be down; RETRYING next cycle, position still open")
        self.state.save()

    def _revalidate_protective_stops(self) -> None:
        """Startup: re-arm the native protective stop on every open real futures
        position. A persisted protective_order_id can be stale — filled or
        cancelled while the bot was down — and the gateway offers no
        order-status lookup to verify it, so cancel-and-replace
        deterministically: cancel the old id (a no-op if it's already gone)
        and place a fresh stop at the position's stop price. Without this, a
        position that survives a restart is protected by nothing but the EOD
        flatten."""
        if not self._live_projectx():
            return
        broker = self.executor.broker
        touched = False
        for pos in self.state.open_positions:
            if pos.shadow or not is_futures_symbol(pos.symbol):
                continue
            if not pos.stop or pos.stop <= 0:
                notify(f"⚠ {pos.symbol} open with NO stop price — cannot re-arm a "
                       f"native stop; only the EOD flatten protects it")
                continue
            old = getattr(pos, "protective_order_id", "")
            if old:
                try:
                    broker.cancel_order(old)
                except Exception:  # noqa: BLE001 — stale id already gone is the normal case
                    pass
            stop_side = "SELL" if pos.side == "BUY" else "BUY"
            try:
                oid = broker.place_stop_order(pos.symbol, int(pos.qty), stop_side, pos.stop)
            except Exception as e:  # noqa: BLE001
                oid = ""
                notify(f"⚠ {pos.symbol} stop re-arm FAILED ({e}) — position has no "
                       f"native stop; client-side management only")
            if oid:
                pos.protective_order_id = oid
                touched = True
                notify(f"🛡 re-armed native stop {pos.symbol} {stop_side} "
                       f"{int(pos.qty)}x @ {pos.stop:.2f} (restart revalidation)")
            elif old:
                pos.protective_order_id = ""
                touched = True
        if touched:
            self.state.save()

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
        #
        # Qty comparison uses a tolerance of 0.5 contracts rather than direct
        # float equality: futures are always whole-number contracts, so any
        # difference < 0.5 is floating-point noise (same integer quantity),
        # and any difference ≥ 0.5 is a genuine size change that must be
        # adopted. This avoids phantom-closing or adopting a position whose
        # qty is 2.0 locally vs 2.0000001 from the broker (or vice versa).
        _QTY_TOL = 0.5  # anything less than half a contract is noise
        for pos in list(self.state.open_positions):
            if pos.shadow:
                continue
            bp = bro.get(pos.symbol.upper())
            b_side = str(bp.get("side", "")).upper() if bp else ""
            b_qty = float(bp.get("qty") or 0.0) if bp else 0.0
            if bp is not None and b_side == pos.side.upper() \
                    and abs(b_qty - pos.qty) < _QTY_TOL:
                continue        # exact match (within float noise) — nothing to do
            if bp is not None and b_side == pos.side.upper() \
                    and b_qty > 0 and abs(b_qty - pos.qty) >= _QTY_TOL:
                # Same direction, materially different size (partial fill /
                # external reduction): adopt the broker's qty as truth.
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
            if getattr(pos, "protective_order_id", ""):
                # Position vanished while a native stop was resting — the stop
                # (or Topstep liquidation) presumably took it out. Count it:
                # the stop-execution audit needs these visible, not folklore.
                from exec_telemetry import TELEM
                TELEM.record("stop_assumed_filled", symbol=pos.symbol,
                             exit_price=exit_price, pnl_usd=round(pos.pnl_usd, 2))
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
        # H-CRIT-2: this pass just re-established ground truth for every
        # local + broker position, so any exit that was previously held for
        # being ambiguous is now safe to act on again — either it matches
        # broker reality (retry the exit normally) or reconcile already fixed
        # the local book above (phantom-closed / orphan-adopted).
        if self._exit_ambiguous:
            notify(f"♻ reconcile: clearing {len(self._exit_ambiguous)} ambiguous-exit "
                   f"hold(s) — {sorted(self._exit_ambiguous)}")
        self._exit_ambiguous.clear()

    def _topstep_flatten_all(self, reason: str) -> None:
        """Market-close every open real position via ProjectX and book only the
        ones the BROKER confirms flat. Used by the breach handler and the EOD
        flatten window, both of which call this every scan — so a partial or
        failed flatten leaves the unconfirmed positions in local state and the
        next scan retries, rather than one-shot booking a phantom flat."""
        from projectx_executor import ProjectXBroker
        broker = self.executor.broker
        if not isinstance(broker, ProjectXBroker):
            return
        open_futures = [p for p in self.state.open_positions if not p.shadow]
        if not open_futures:
            return
        notify(f"[Topstep] flatten ({reason}) — {len(open_futures)} position(s) "
               f"via ProjectX")
        # Cancel resting protective stops BEFORE closing: a stop left working
        # after the flatten would fill on the next trade-through and open a
        # brand-new naked position on a halted account.
        for pos in open_futures:
            pid = getattr(pos, "protective_order_id", "")
            if pid:
                try:
                    broker.cancel_order(pid)
                    pos.protective_order_id = ""
                except Exception as e:  # noqa: BLE001
                    notify(f"  ! stop cancel failed {pos.symbol} ({pid}): {e}")
        # Submit the flatten. flatten_all() raises on a get_positions() outage
        # (unknown broker state) — do NOT book any close on that; retry next scan.
        try:
            broker.flatten_all()
        except Exception as e:  # noqa: BLE001
            notify(f"  ! Topstep flatten submit failed: {e} — retry next scan")
            return
        # Confirm against the broker: only book positions it now reports FLAT.
        # If we can't read positions, leave the whole local book open to retry.
        try:
            still_open = {str(p.get("symbol", "")).upper()
                          for p in broker.get_positions()}
        except Exception as e:  # noqa: BLE001
            notify(f"  ! Topstep flatten confirm failed: {e} — book left open, retry next scan")
            return
        for pos in open_futures:
            if pos.symbol.upper() in still_open:
                notify(f"  ! {pos.symbol} still open at broker after flatten — retry next scan")
                continue
            mark = self._mark(pos.symbol)
            exit_price = mark if mark is not None else pos.entry_price
            self.state.close(pos, exit_price)
            self._topstep.record_close(pos.pnl_usd, hold_seconds(pos.opened_at))
            notify(f"  TOPSTEP-FLATTEN {pos.symbol} qty={pos.qty} @ ~{exit_price:.2f}")
            if CONFIG.manual_tickets:
                notify(exit_ticket(pos, exit_price, reason))

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
        if pos.symbol in self._exit_ambiguous:
            return  # unresolved ambiguous exit (H-CRIT-2) — wait for reconcile
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
                try:
                    ok = self.executor.close_partial(pos, close_qty, mark)
                except Exception as e:  # noqa: BLE001
                    if e.__class__.__name__ == "OrderStateUnknown":
                        notify(f"  ⚠ {pos.symbol} PARTIAL-EXIT order state UNKNOWN "
                               f"(transport error after send) — NOT retried; holding "
                               f"until next reconcile confirms broker truth")
                        self._exit_ambiguous.add(pos.symbol)
                    else:
                        notify(f"  ! partial exit failed {pos.symbol}: {e}")
                    ok = False
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
            if pos.symbol in self._exit_ambiguous:
                continue  # unresolved ambiguous exit (H-CRIT-2) — wait for reconcile
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
                if e.__class__.__name__ == "OrderStateUnknown":
                    # H-CRIT-2: the exit order may have already reached the
                    # exchange despite the transport failure. Blindly retrying
                    # next cycle (the old behavior) risks a duplicate exit or
                    # flipping this position to the opposite side if the
                    # ambiguous order actually filled. Hold it until the next
                    # _reconcile_positions() pass resolves the broker's real
                    # state before touching this symbol again.
                    notify(f"  ⚠ {pos.symbol} EXIT order state UNKNOWN (transport "
                           f"error after send) — NOT retried; holding until next "
                           f"reconcile confirms broker truth")
                    self._exit_ambiguous.add(pos.symbol)
                else:
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
