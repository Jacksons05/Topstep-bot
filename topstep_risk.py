"""Topstep $50K funded-account risk rules.

Layers on top of the existing risk.py stack — it never replaces or disables
the base rules (daily drawdown %, circuit breakers, kill switch, cooldowns).
Instead it enforces the Topstep $50K Trading Combine / Express Funded Account
spec (No-Activation-Fee path, Responsible Trading Advantage ON):

  1. Trailing Maximum Loss Limit  (HARD FAIL RULE)
       Topstep's only hard rule. The MLL starts $2,000 below the account
       start ($48,000 for a $50K account) and trails UP intraday in real time
       on combined realized + unrealized equity. It LOCKS permanently at the
       starting balance ($50,000) once peak equity reaches start + buffer
       ($52,000). If live equity ever touches the floor → account liquidated.
       We mirror it: track intraday peak equity, floor = min(peak - buffer,
       account_size); block new entries (and flatten) as equity approaches it.

  2. Daily Loss Limit  (Responsible Trading Advantage)
       $1,000 for the $50K. Hitting it deactivates the DAY (not the account).
       Measured as today's realized + open P&L vs the day's starting balance.

  3. Max Contracts (ACCOUNT-WIDE)
       5 minis for the $50K (50 micros at the 10:1 ratio). This is a total
       across all symbols, NOT per-symbol.

  4. Consistency Rule  (payout eligibility)
       Best single day ≤ 50% of cumulative profit. Enforced live as a per-day
       profit cap: stop opening NEW trades once today's profit reaches
       consistency_pct * profit_target ($1,500). Exits are never blocked.

  5. EOD Position Flatten + Economic Release Blackout
       Flatten all by TOPSTEP_FLATTEN_TIME (16:08 ET, before the 16:10 futures
       close) to avoid carrying unrealized risk against the trailing MLL.
       No new entries within TOPSTEP_ECON_BLACKOUT_MIN of a major macro release.

NOTE: Topstep does NOT ban scalping — the legacy Topstep ≤5s microscalp guard is
DORMANT (TOPSTEP_SCALP_GUARD_ENABLED=False) but kept for optional use + tests.

Usage in engine.py:
    from topstep_risk import TopstepRiskManager
    ts = TopstepRiskManager(initial_equity=<live balance>)

    # each scan, before the check() call:
    ts.update_equity(equity)
    breached, why = ts.risk_breach(equity, state, unrealized)   # → flatten + halt day
    if not ts.pre_trade_ok(sig, state, equity, unrealized):
        continue
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from config import CONFIG
from futures_symbols import mini_equivalents
from signals import Signal
from state import State

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")


# ── Hardcoded high-impact economic release times (ET) ─────────────────────
# These are the REGULARLY SCHEDULED times for major US macro prints.
# Exceptions (month-end moves, special sessions) are NOT captured here.
# For production use, supplement with a live economic calendar API.
#
# Format: (hour, minute)  — all Eastern Time
_HIGH_IMPACT_RELEASE_TIMES: list[tuple[int, int]] = [
    # 8:30 AM — most major prints: NFP, CPI, PPI, GDP, Retail Sales,
    #           Initial Claims, Empire State Mfg, Philly Fed, etc.
    (8, 30),
    # 9:00 AM — Case-Shiller, FHFA Home Price (monthly)
    (9, 0),
    # 9:45 AM — S&P PMI (flash, monthly)
    (9, 45),
    # 10:00 AM — ISM, JOLTS, Consumer Confidence, Existing Home Sales,
    #            Leading Indicators, Michigan Sentiment (final)
    (10, 0),
    # 10:30 AM — EIA Petroleum Status (weekly, Wednesdays)
    (10, 30),
    # 2:00 PM — FOMC Rate Decision (eight times per year)
    (14, 0),
    # 2:30 PM — FOMC Chair press conference (follows decision days)
    (14, 30),
]


def _now_et() -> datetime:
    return datetime.now(_ET)


def _parse_time(hhmm: str) -> dtime:
    """Parse "HH:MM" string to a time object.

    Falls back to 16:00 ET on a malformed value — deliberately EARLIER than
    the flatten-time default (16:08 ET, itself before the 16:10 futures
    close): a parse failure must fail toward flattening too early, never
    toward flattening late enough to carry a position into the close.
    """
    try:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return dtime(hh, mm)
    except (ValueError, AttributeError):
        log.warning(f"[TopstepRisk] Could not parse time '{hhmm}', using 16:00 (conservative)")
        return dtime(16, 0)


def hold_seconds(opened_at: str) -> float:
    """Seconds a position has been open. `opened_at` is a UTC ISO string
    (see executor._now). Returns +inf on parse failure so callers fail OPEN:
    a bad timestamp must never delay a profit exit (treat it as held long
    enough) nor count the trade as a ≤5s scalp."""
    try:
        opened = datetime.fromisoformat(opened_at)
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - opened).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


class TopstepRiskManager:
    """Risk layer enforcing the Topstep $50K funded-account spec.

    Most methods are pure functions of the current equity / state snapshot.
    The instance carries two pieces of session state:
      * day_start_equity — anchors the Daily Loss Limit (reset each day).
      * peak_equity       — highest combined (realized+unrealized) equity seen
                            this account cycle; drives the TRAILING Max Loss
                            Limit. It is NOT reset daily — the MLL trails across
                            the whole cycle and locks once it reaches the start
                            balance. update_equity() must be called each scan.
    """

    def __init__(self, initial_equity: float | None = None,
                 historical_peak: float | None = None) -> None:
        # Live equity at startup. Falls back to the configured account size.
        start = initial_equity or CONFIG.topstep_account_size
        self.account_size: float = CONFIG.topstep_account_size
        self.mll_buffer: float = CONFIG.topstep_trailing_mll
        self.day_start_equity: float = start
        # Peak equity seed (Bug #7 fix): the CURRENT balance is only a valid
        # peak on a genuinely fresh account. On a cold boot after a drawdown,
        # seeding from the balance drags the trailing-MLL floor DOWN $2k below
        # wherever the account sits — looser than the house floor Topstep has
        # already locked. So the seed is max(current, account_size, HISTORICAL
        # max) where historical_peak comes from Postgres account_history
        # (state.historical_peak_equity, keyed by broker account id); None
        # means no history exists — a fresh cycle — and only then does the
        # balance-based seed stand alone. load_day_state() may still raise the
        # peak further from the local JSON snapshot; both sources are ratchets,
        # never reseeds.
        self.peak_equity: float = max(start, self.account_size,
                                      historical_peak or 0.0)
        self._peak_seed_source: str = (
            "db_history" if historical_peak is not None
            and historical_peak >= max(start, self.account_size)
            else "balance/account_size"
        )
        # Microscalping attribution (reset each day): gross realized profit from
        # winning trades, and the slice of it earned on trades held ≤Ns.
        self._win_profit_total: float = 0.0
        self._win_profit_scalp: float = 0.0
        self._min_days_warned: bool = False
        self._mll_breach_warned: bool = False
        # Set by load_day_state() when persisted risk state was EXPECTED (the
        # day-state file exists) but is unreadable/corrupt. In that case the
        # __init__ peak seed above may sit BELOW the true locked high-water mark,
        # silently loosening the trailing-MLL floor — so we fail closed (block
        # entries + preflight FAIL) rather than trade on a reseeded peak.
        self._cold_start_unsafe: bool = False
        log.info(
            f"[Topstep] initialized | account=${self.account_size:,.0f} | "
            f"start_equity=${start:,.2f} | peak_seed=${self.peak_equity:,.2f} "
            f"({self._peak_seed_source}) | trailing_MLL=${self.mll_buffer:,.0f} "
            f"(floor=${self.mll_floor():,.2f}) | daily_loss_limit="
            f"${CONFIG.topstep_daily_loss_limit:,.0f} "
            f"(responsible_trading={CONFIG.topstep_responsible_trading}) | "
            f"max_contracts={CONFIG.topstep_max_contracts} (account-wide) | "
            f"profit_target=${CONFIG.topstep_profit_target:,.0f} | "
            f"consistency={CONFIG.topstep_consistency_pct:.0%} | "
            f"flatten_at={CONFIG.topstep_flatten_time} ET"
        )

    def giveback_ok(self, equity: float) -> tuple[bool, str]:
        """Profit give-back guard: entries-only soft halt while equity is more
        than topstep_giveback_halt_usd below the cycle peak. Never flattens —
        open positions keep managing; the halt lifts as equity recovers or the
        cap is disabled (0). Only meaningful once peak is above the locked MLL
        floor (i.e. there IS profit to protect)."""
        cap = CONFIG.topstep_giveback_halt_usd
        if cap <= 0:
            return True, "ok"
        if self.peak_equity <= self.account_size + self.mll_buffer:
            return True, "ok"        # floor not locked yet — MLL still trails
        drawdown = self.peak_equity - equity
        if drawdown >= cap:
            return False, (f"profit give-back halt: equity ${equity:,.2f} is "
                           f"${drawdown:,.2f} below peak ${self.peak_equity:,.2f} "
                           f"(cap ${cap:,.0f}) — new entries blocked until recovery")
        return True, "ok"

    # ── Restart persistence ──────────────────────────────────────────────────
    # day_start_equity (DLL anchor), peak_equity (trailing-MLL high-water mark)
    # and the day-halt flag must survive a process restart: without this every
    # restart re-arms a fresh Daily Loss Limit allowance and reseeds the MLL
    # peak from the current balance, so repeated restarts turn the $1k/day
    # limit into an unbounded daily loss.
    _DAY_STATE_FILE = Path(__file__).with_name("topstep_day_state.json")

    def save_day_state(self, session_date: str, day_halt: bool) -> None:
        """Persist the session anchors. Call on day reset and whenever the
        engine sets/clears its day-halt flag."""
        try:
            self._DAY_STATE_FILE.write_text(json.dumps({
                "session_date": session_date,
                "day_start_equity": self.day_start_equity,
                "peak_equity": self.peak_equity,
                "day_halt": day_halt,
            }))
        except OSError as exc:
            log.warning(f"[Topstep] day-state save failed: {exc}")

    def load_day_state(self, session_date: str) -> tuple[bool, bool]:
        """Restore persisted anchors at startup. peak_equity is restored
        unconditionally (the trailing MLL spans the whole account cycle);
        day_start_equity and the halt flag apply only when the saved session
        matches the current one. Returns (session_restored, day_halt).

        A GENUINELY ABSENT file (fresh account/cycle) is fine: the __init__ peak
        seed (account_size) is the correct starting floor. But a file that is
        PRESENT yet corrupt/unreadable means state was EXPECTED and lost — the
        seed may understate the true locked high-water mark and loosen the
        trailing-MLL floor. That case fails closed (sets _cold_start_unsafe, so
        preflight FAILs and the engine won't arm entries) instead of silently
        trading on a reseeded peak.
        """
        if not self._DAY_STATE_FILE.exists():
            # No persisted state expected — genuine fresh account/cycle.
            return False, False
        # File is present → state was expected. Any failure to read a usable
        # peak_equity from here is a lost high-water mark, not a fresh start.
        try:
            d = json.loads(self._DAY_STATE_FILE.read_text())
        except (OSError, ValueError) as exc:
            self._cold_start_unsafe = True
            log.error(f"[Topstep] day-state file present but corrupt/unreadable "
                      f"({exc}) — FAIL CLOSED: entries blocked until resolved")
            return False, False
        if not isinstance(d, dict):
            # Valid JSON but wrong structure (null, list, string, …).
            self._cold_start_unsafe = True
            log.error("[Topstep] day-state file present but non-dict JSON — "
                      "FAIL CLOSED: entries blocked until resolved")
            return False, False
        try:
            pk = float(d.get("peak_equity") or 0.0)
        except (TypeError, ValueError):
            pk = 0.0
        if pk <= 0:
            # File present but no usable high-water mark — same risk as corrupt.
            self._cold_start_unsafe = True
            log.error("[Topstep] day-state present but peak_equity missing/<=0 — "
                      "FAIL CLOSED: entries blocked until resolved")
            return False, False
        if pk > self.peak_equity:
            self.peak_equity = pk
        if str(d.get("session_date")) != session_date:
            return False, False
        try:
            dse = float(d.get("day_start_equity") or 0.0)
        except (TypeError, ValueError):
            dse = 0.0
        if dse > 0:
            self.day_start_equity = dse
        log.info(f"[Topstep] restored session state | day_base=${self.day_start_equity:,.2f} "
                 f"| peak=${self.peak_equity:,.2f} | halt={bool(d.get('day_halt'))}")
        return True, bool(d.get("day_halt"))

    def cold_start_unsafe(self) -> bool:
        """True when persisted risk state was expected (day-state file present)
        but missing/corrupt at startup — the trailing-MLL peak may be understated,
        so entries must stay blocked (fail closed) until an operator resolves it.
        Set by load_day_state()."""
        return self._cold_start_unsafe

    def reset_day(self, current_equity: float) -> None:
        """Call at the start of each new trading day to anchor the Daily Loss
        Limit. Does NOT reset peak_equity — the trailing MLL spans the cycle."""
        self.day_start_equity = current_equity
        self.peak_equity = max(self.peak_equity, current_equity)
        self._win_profit_total = 0.0
        self._win_profit_scalp = 0.0
        self._mll_breach_warned = False
        log.info(f"[Topstep] day reset | day_base=${current_equity:,.2f} | "
                 f"peak=${self.peak_equity:,.2f} | floor=${self.mll_floor():,.2f}")

    # ── Trailing Maximum Loss Limit (the one HARD Topstep rule) ─────────────

    def update_equity(self, equity: float) -> None:
        """Ratchet the intraday peak equity. Call once per scan with the best
        available equity estimate (realized + unrealized)."""
        if equity > self.peak_equity:
            self.peak_equity = equity

    def mll_floor(self) -> float:
        """Current trailing MLL floor. Trails up with peak equity but locks at
        the starting balance (account_size) once peak ≥ account_size + buffer."""
        return min(self.peak_equity - self.mll_buffer, self.account_size)

    def trailing_mll_ok(self, equity: float) -> tuple[bool, str]:
        """False (= breach) when live equity has touched/crossed the MLL floor."""
        floor = self.mll_floor()
        if equity <= floor:
            reason = (
                f"Topstep trailing MLL breached: equity=${equity:,.2f} "
                f"<= floor=${floor:,.2f} (peak=${self.peak_equity:,.2f}, "
                f"buffer=${self.mll_buffer:,.0f})"
            )
            if not self._mll_breach_warned:
                log.warning(f"[Topstep] LIQUIDATE: {reason}")
                self._mll_breach_warned = True
            return False, reason
        self._mll_breach_warned = False
        return True, "ok"

    # ── Microscalping guard (Topstep: >50% of profit from ≤5s holds is banned) ──
    def record_close(self, pnl_usd: float, held_sec: float) -> None:
        """Record a closed trade for scalp-profit attribution. Only winning
        trades feed the pool — the firm rule is about the source of *profit*."""
        if pnl_usd <= 0:
            return
        self._win_profit_total += pnl_usd
        if held_sec <= CONFIG.topstep_min_profit_hold_sec:
            self._win_profit_scalp += pnl_usd

    def scalp_profit_share(self) -> float:
        """Fraction of winning profit earned on ≤5s holds (0.0 if no wins yet)."""
        if self._win_profit_total <= 0:
            return 0.0
        return self._win_profit_scalp / self._win_profit_total

    def scalp_profit_ok(self) -> tuple[bool, str]:
        """Block NEW entries once ≤5s winners dominate realized profit, before
        the Topstep 0.50 hard cap is breached. Returns (ok, reason)."""
        share = self.scalp_profit_share()
        if share >= CONFIG.topstep_scalp_profit_pct_limit:
            reason = (
                f"Topstep microscalp guard: {share:.0%} of profit from ≤"
                f"{CONFIG.topstep_min_profit_hold_sec:g}s holds "
                f"(limit {CONFIG.topstep_scalp_profit_pct_limit:.0%}, firm cap 50%)"
            )
            return False, reason
        return True, "ok"

    def profit_exit_held_long_enough(self, opened_at: str) -> bool:
        """True if a take-profit exit is allowed now (held ≥ min hold). Callers
        MUST bypass this for stop-losses — never delay a risk exit."""
        return hold_seconds(opened_at) >= CONFIG.topstep_min_profit_hold_sec

    # ── Daily Loss Limit (Responsible Trading Advantage) ────────────────────

    def daily_loss_ok(self, equity: float | None = None, state: State | None = None,
                      unrealized: float = 0.0) -> tuple[bool, str]:
        """Check today's P&L against the Topstep Daily Loss Limit.

        Preferred measure: live combined equity minus the equity anchored at the
        session reset (reset_day) — day_pnl = equity - day_start_equity. This is
        independent of state.py's UTC day-roll, so the limit is measured against
        the real Topstep session boundary (set by the engine's session-date reset).
        Falls back to the realized+unrealized measure when no equity is supplied
        (keeps older callers / tests working).

        Hitting the limit deactivates the DAY (engine flattens + halts new entries
        until the next session). Off entirely if Responsible Trading is disabled.
        Returns (ok, reason). ok=False means the day is done.
        """
        if not CONFIG.topstep_responsible_trading:
            return True, "ok"
        limit = CONFIG.topstep_daily_loss_limit
        if equity is not None:
            day_pnl = equity - self.day_start_equity
        else:
            day_pnl = (state.daily_pnl() if state is not None else 0.0) + unrealized
        if day_pnl <= -limit:
            reason = (
                f"Topstep daily loss limit hit: day_pnl=${day_pnl:,.2f} "
                f"<= -${limit:,.0f} — day deactivated"
            )
            log.warning(f"[Topstep] DAY HALT: {reason}")
            return False, reason
        return True, "ok"

    # ── Max Contracts (ACCOUNT-WIDE, all symbols) ───────────────────────────

    def contracts_ok(self, symbol: str | None, state: State) -> tuple[bool, str]:
        """Enforce the account-wide max-contracts cap (TOPSTEP_MAX_CONTRACTS).

        Topstep limits TOTAL open contracts across every symbol, not per-name.
        Counts the real book only (shadow/cramer positions have no exposure).
        `symbol` is accepted for call-site compatibility but not used for the
        cap (the limit is account-wide).
        """
        limit = CONFIG.topstep_max_contracts
        ratio = CONFIG.topstep_micro_ratio
        # Count in MINI-equivalents: the Topstep cap is denominated in minis
        # (5 minis = 50 micros @ 10:1). Counting raw qty blocked a micro-only
        # book at 5 raw micros (10× too tight) and would UNDER-count on a mix.
        n = sum(mini_equivalents(p.symbol, int(p.qty), ratio)
                for p in state.open_positions if not p.shadow)
        if n >= limit:
            reason = (f"Topstep max contracts: {n:.1f} mini-equiv open "
                      f"account-wide, limit={limit}")
            log.info(f"[Topstep] block: {reason}")
            return False, reason
        return True, "ok"

    # ── Consistency rule (best day ≤ pct of cumulative profit) ──────────────

    def consistency_ok(self, state: State) -> tuple[bool, str]:
        """Stop opening NEW trades once today's profit reaches
        consistency_pct * profit_target (the per-day cap that keeps any single
        day ≤ 50% of the cumulative profit needed to pass). Never blocks exits.
        """
        cap = CONFIG.topstep_consistency_pct * CONFIG.topstep_profit_target
        day_profit = state.daily_pnl()
        if day_profit >= cap:
            reason = (
                f"Topstep consistency cap: today +${day_profit:,.2f} "
                f">= ${cap:,.0f} ({CONFIG.topstep_consistency_pct:.0%} of "
                f"${CONFIG.topstep_profit_target:,.0f} target) — no new entries today"
            )
            log.info(f"[Topstep] block: {reason}")
            return False, reason
        return True, "ok"

    # ── 3. EOD Position Flatten ─────────────────────────────────────────────

    def should_flatten_now(self) -> bool:
        """True when it's time to flatten all positions before EOD.

        Flatten fires at TOPSTEP_FLATTEN_TIME (default 16:08 ET, matching
        config.py's default) and stays True until 17:00 ET (the CME
        maintenance close) — NOT until midnight; the 18:00 ET Globex reopen
        is a distinct, later boundary where new entries become allowed again
        (see engine._market_open). The engine calls this once per scan loop
        and submits flatten_all() on the broker when True.

        Note: The existing OPTION_TIME_STOP_ET (15:30 by default) handles
        options; this rule handles futures.
        """
        import cme_calendar

        flatten_t = _parse_time(CONFIG.topstep_flatten_time)
        # Flatten window: flatten_time → 17:00 ET (CME maintenance close).
        # After 18:00 ET the overnight Globex session is open — new entries allowed.
        close_t = _parse_time("17:00")
        now = _now_et()
        current_t = now.time()
        # Early-close day: the day session halts hours before the normal close,
        # so the default 16:08 flatten would fire AFTER the exchange has already
        # halted — pull the window forward to (halt − safety margin).
        halt = cme_calendar.early_close_halt(now.date())
        if halt is not None:
            early = (datetime.combine(now.date(), halt)
                     - timedelta(minutes=cme_calendar.EARLY_CLOSE_FLATTEN_MARGIN_MIN)).time()
            flatten_t = min(flatten_t, early)
        result = flatten_t <= current_t < close_t
        if result:
            log.debug(
                f"[TopstepRisk] flatten window active: "
                f"{current_t} >= {flatten_t} ET"
            )
        return result

    # ── 4. Economic Release Blackout ────────────────────────────────────────

    def near_economic_release(self, blackout_min: int = 5) -> tuple[bool, str]:
        """True if we're within `blackout_min` minutes of a major econ release.

        Default window: 5 minutes before or after release time.
        To widen the window, pass a larger blackout_min, or set via config
        by extending this class.

        Note: The existing event_blackout in events.py (using Finnhub) already
        covers earnings and macro events within EVENT_BLACKOUT_HOURS. This
        adds a tighter per-minute check specifically for the releases that
        create violent futures moves (NFP, CPI, FOMC).
        """
        now = _now_et()
        now_mins = now.hour * 60 + now.minute

        for hh, mm in _HIGH_IMPACT_RELEASE_TIMES:
            release_mins = hh * 60 + mm
            delta = abs(now_mins - release_mins)
            if delta <= blackout_min:
                rel_str = f"{hh:02d}:{mm:02d} ET"
                reason = (
                    f"within {delta} min of scheduled release at {rel_str} "
                    f"(blackout window ±{blackout_min} min)"
                )
                log.info(f"[TopstepRisk] econ blackout: {reason}")
                return True, reason

        return False, "ok"

    # ── Hard breach check (call each scan → flatten + halt the day) ─────────

    def risk_breach(self, equity: float, state: State,
                    unrealized: float = 0.0) -> tuple[bool, str]:
        """True when a Topstep account-protection limit is breached and the
        engine MUST flatten everything immediately:
          * trailing Maximum Loss Limit (account-fail — never recoverable)
          * Daily Loss Limit (day deactivated until next session)

        Distinct from pre_trade_ok, which only blocks NEW entries. Stop-losses
        and this breach flatten always take priority over any hold logic.
        """
        mll_ok, mll_reason = self.trailing_mll_ok(equity)
        if not mll_ok:
            return True, mll_reason
        dll_ok, dll_reason = self.daily_loss_ok(equity, state, unrealized)
        if not dll_ok:
            return True, dll_reason
        return False, "ok"

    def check_combine_progress(self, state: State) -> None:
        """Warn once if the profit target is hit before the Combine's minimum
        active-trading-days requirement is satisfied — the P&L number alone
        can look like a pass when it isn't yet."""
        if self._min_days_warned:
            return
        days = len(state.trading_days)
        if (state.realized_pnl_usd >= CONFIG.topstep_profit_target
                and days < CONFIG.topstep_min_trading_days):
            self._min_days_warned = True
            log.warning(
                f"[Topstep] profit target (${CONFIG.topstep_profit_target:,.0f}) hit "
                f"but only {days}/{CONFIG.topstep_min_trading_days} active trading days "
                "logged — Combine not yet passable on the min-days rule."
            )

    # ── Combined pre-trade gate ─────────────────────────────────────────────

    def pre_trade_ok(self, sig: Signal, state: State, equity: float | None = None,
                     unrealized: float = 0.0) -> tuple[bool, str]:
        """Single call that runs all Topstep checks before opening a new trade.

        Returns (ok, reason). Called in engine.run_once() BEFORE the existing
        risk.check() call so Topstep blocks stack on top of the base rules.
        `equity` should be the live realized+unrealized account equity; when
        omitted it falls back to account_size + day P&L + unrealized.

        Check order (fast / cheap checks first):
          1. EOD flatten window active → block all new entries
          2. Economic release blackout
          3. Trailing Maximum Loss Limit (hard)
          4. Daily Loss Limit (Responsible Trading)
          5. Account-wide contract cap
          6. Consistency per-day profit cap
          7. Microscalping guard (only if TOPSTEP_SCALP_GUARD_ENABLED)
        """
        if equity is None:
            equity = self.account_size + state.daily_pnl() + unrealized

        # 1. Flatten window: if we should be flat, we shouldn't be opening
        if self.should_flatten_now():
            return False, "Topstep flatten window active (≥ TOPSTEP_FLATTEN_TIME)"

        # 2. Economic release blackout
        near_rel, rel_reason = self.near_economic_release(CONFIG.topstep_econ_blackout_min)
        if near_rel:
            return False, f"Topstep econ blackout: {rel_reason}"

        # 3. Trailing MLL (hard fail rule)
        mll_ok, mll_reason = self.trailing_mll_ok(equity)
        if not mll_ok:
            return False, mll_reason

        # 4. Daily loss limit
        dll_ok, dll_reason = self.daily_loss_ok(equity, state, unrealized)
        if not dll_ok:
            return False, dll_reason

        # 4b. Profit give-back guard (ours): block NEW entries while equity sits
        # too far below the cycle peak. Topstep's MLL locks at account_size, so
        # accumulated profit above it has no house protection at all.
        gb_ok, gb_reason = self.giveback_ok(equity)
        if not gb_ok:
            return False, gb_reason

        # 5. Account-wide contract cap
        contr_ok, contr_reason = self.contracts_ok(sig.symbol, state)
        if not contr_ok:
            return False, contr_reason

        # 6. Consistency per-day profit cap
        cons_ok, cons_reason = self.consistency_ok(state)
        if not cons_ok:
            return False, cons_reason

        # 7. Microscalping guard — dormant under Topstep unless explicitly enabled
        if CONFIG.topstep_scalp_guard_enabled:
            scalp_ok, scalp_reason = self.scalp_profit_ok()
            if not scalp_ok:
                return False, scalp_reason

        return True, "ok"


# ── Convenience module-level functions (for callers that don't hold an instance) ──

def should_flatten_now() -> bool:
    """Module-level alias for quick one-off checks without an instance."""
    return TopstepRiskManager().should_flatten_now()


def near_economic_release(blackout_min: int = 5) -> tuple[bool, str]:
    """Module-level alias for one-off checks."""
    return TopstepRiskManager().near_economic_release(blackout_min)
