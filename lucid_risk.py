"""Lucid Trading-specific risk rules.

Layers on top of the existing risk.py stack — it never replaces or disables
the base rules (daily drawdown %, circuit breakers, kill switch, cooldowns).
Instead it adds Lucid-specific constraints that make sense for a funded
futures account:

  1. EOD Drawdown Kill-Switch
       Lucid measures drawdown at end-of-day against the prior day's EOD
       balance, not on an intraday trailing basis. We mirror that here:
       LUCID_DAILY_DRAWDOWN_USD is the maximum dollar loss for the day before
       new entries are blocked. (Separate from DAILY_DRAWDOWN_PCT which is
       the intraday % guard.)

  2. Max Contracts Per Symbol
       Hard cap on open contracts per futures root. Default: 3 per symbol
       (LUCID_MAX_CONTRACTS). Prevents over-sizing in a single name.

  3. EOD Position Flatten
       All open positions must be flat by LUCID_FLATTEN_TIME (default 16:55
       ET). This fires BEFORE the Lucid EOD drawdown calculation window.
       The engine calls should_flatten_now() once per scan loop and, when
       True, calls rithmic_broker.flatten_all() on the open book.

  4. Economic Release Blackout
       No new entries within LUCID_ECON_BLACKOUT_MIN (default 5) minutes
       before or after a major scheduled economic release. Checks against a
       hardcoded list of high-impact release times; for live use you should
       back this with a live economic calendar (Finnhub or similar).

Usage in engine.py:
    from lucid_risk import LucidRiskManager
    lucid = LucidRiskManager()

    # In run_once(), before the existing check() call:
    if not lucid.pre_trade_ok(sig, state):
        continue
    # In _manage_open() or just before the scan loop:
    if lucid.should_flatten_now():
        executor.broker.flatten_all()
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

from config import CONFIG
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
    """Parse "HH:MM" string to a time object. Falls back to 16:55 on error."""
    try:
        hh, mm = (int(x) for x in hhmm.split(":"))
        return dtime(hh, mm)
    except (ValueError, AttributeError):
        log.warning(f"[LucidRisk] Could not parse time '{hhmm}', using 16:55")
        return dtime(16, 55)


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


class LucidRiskManager:
    """Stateless-ish risk layer for Lucid-funded futures accounts.

    Most methods are pure functions of the current time / state snapshot.
    The instance holds a `day_start_equity` reference that is captured once
    at construction (or reset via reset_day()) so EOD drawdown math is
    consistent within a session.
    """

    def __init__(self, initial_equity: float | None = None) -> None:
        # Base equity at the start of the trading day — used to calculate
        # the Lucid EOD drawdown limit. Capture from account on startup.
        # If None, we use CONFIG.bankroll_usd as a safe default until the
        # engine provides the real live balance.
        self.day_start_equity: float = initial_equity or CONFIG.bankroll_usd
        # Microscalping attribution (reset each day): gross realized profit from
        # winning trades, and the slice of it earned on trades held ≤5s.
        self._win_profit_total: float = 0.0
        self._win_profit_scalp: float = 0.0
        log.info(
            f"[LucidRisk] initialized | day_start_equity=${self.day_start_equity:,.2f} | "
            f"drawdown_limit=${CONFIG.lucid_daily_drawdown_usd:,.2f} | "
            f"max_contracts={CONFIG.lucid_max_contracts} | "
            f"flatten_at={CONFIG.lucid_flatten_time} | "
            f"min_profit_hold={CONFIG.lucid_min_profit_hold_sec}s | "
            f"scalp_profit_limit={CONFIG.lucid_scalp_profit_pct_limit:.0%}"
        )

    def reset_day(self, current_equity: float) -> None:
        """Call at the start of each new trading day to anchor the drawdown base."""
        self.day_start_equity = current_equity
        self._win_profit_total = 0.0
        self._win_profit_scalp = 0.0
        log.info(f"[LucidRisk] day reset | new base=${current_equity:,.2f}")

    # ── Microscalping guard (Lucid: >50% of profit from ≤5s holds is banned) ──
    def record_close(self, pnl_usd: float, held_sec: float) -> None:
        """Record a closed trade for scalp-profit attribution. Only winning
        trades feed the pool — the firm rule is about the source of *profit*."""
        if pnl_usd <= 0:
            return
        self._win_profit_total += pnl_usd
        if held_sec <= CONFIG.lucid_min_profit_hold_sec:
            self._win_profit_scalp += pnl_usd

    def scalp_profit_share(self) -> float:
        """Fraction of winning profit earned on ≤5s holds (0.0 if no wins yet)."""
        if self._win_profit_total <= 0:
            return 0.0
        return self._win_profit_scalp / self._win_profit_total

    def scalp_profit_ok(self) -> tuple[bool, str]:
        """Block NEW entries once ≤5s winners dominate realized profit, before
        the Lucid 0.50 hard cap is breached. Returns (ok, reason)."""
        share = self.scalp_profit_share()
        if share >= CONFIG.lucid_scalp_profit_pct_limit:
            reason = (
                f"Lucid microscalp guard: {share:.0%} of profit from ≤"
                f"{CONFIG.lucid_min_profit_hold_sec:g}s holds "
                f"(limit {CONFIG.lucid_scalp_profit_pct_limit:.0%}, firm cap 50%)"
            )
            return False, reason
        return True, "ok"

    def profit_exit_held_long_enough(self, opened_at: str) -> bool:
        """True if a take-profit exit is allowed now (held ≥ min hold). Callers
        MUST bypass this for stop-losses — never delay a risk exit."""
        return hold_seconds(opened_at) >= CONFIG.lucid_min_profit_hold_sec

    # ── 1. EOD Drawdown Kill-Switch ────────────────────────────────────────

    def daily_drawdown_ok(self, state: State) -> tuple[bool, str]:
        """Check Lucid EOD-style drawdown against LUCID_DAILY_DRAWDOWN_USD.

        Lucid measures drawdown from the prior session's close, not intraday
        peak. We approximate that here by comparing today's realized + open
        P&L against the day_start_equity captured at session open.

        Returns (ok, reason). ok=False means new entries are blocked.
        """
        limit = CONFIG.lucid_daily_drawdown_usd
        # Total day P&L: realized trades closed today + MTM on open positions.
        day_pnl = state.daily_pnl()   # defined in state.py — realized gains today
        if day_pnl <= -limit:
            reason = (
                f"Lucid EOD drawdown hit: day_pnl=${day_pnl:.2f} "
                f"exceeds limit -${limit:.2f}"
            )
            log.warning(f"[LucidRisk] KILL: {reason}")
            return False, reason
        return True, "ok"

    # ── 2. Max Contracts Per Symbol ─────────────────────────────────────────

    def contracts_ok(self, symbol: str, state: State) -> tuple[bool, str]:
        """Enforce the max-contracts-per-symbol cap (LUCID_MAX_CONTRACTS).

        Counts all open positions in `symbol` (not just the real book —
        shadow/cramer positions are excluded since they have no real exposure).
        """
        limit = CONFIG.lucid_max_contracts
        open_in_sym = [
            p for p in state.open_positions
            if p.symbol == symbol.upper() and not p.shadow
        ]
        n = sum(int(p.qty) for p in open_in_sym)
        if n >= limit:
            reason = (
                f"Lucid max contracts: {n} open in {symbol}, "
                f"limit={limit}"
            )
            log.info(f"[LucidRisk] block: {reason}")
            return False, reason
        return True, "ok"

    # ── 3. EOD Position Flatten ─────────────────────────────────────────────

    def should_flatten_now(self) -> bool:
        """True when it's time to flatten all positions before EOD.

        Flatten fires at LUCID_FLATTEN_TIME (default 16:55 ET) and stays
        True until midnight. The engine calls this once per scan loop and
        submits flatten_all() on the broker when True.

        Note: The existing OPTION_TIME_STOP_ET (15:30 by default) handles
        options; this rule handles futures.
        """
        flatten_t = _parse_time(CONFIG.lucid_flatten_time)
        now = _now_et()
        current_t = now.time()
        result = current_t >= flatten_t
        if result:
            log.debug(
                f"[LucidRisk] flatten window active: "
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
                log.info(f"[LucidRisk] econ blackout: {reason}")
                return True, reason

        return False, "ok"

    # ── Combined pre-trade gate ─────────────────────────────────────────────

    def pre_trade_ok(self, sig: Signal, state: State) -> tuple[bool, str]:
        """Single call that runs all Lucid-specific checks in sequence.

        Returns (ok, reason). Called in engine.run_once() BEFORE the existing
        risk.check() call so Lucid blocks stack on top of the base rules.

        Check order (fast / cheap checks first):
          1. EOD flatten window active → block all new entries
          2. Economic release blackout
          3. EOD drawdown limit
          4. Max contracts per symbol
          5. Microscalping profit-share guard
        """
        # 1. Flatten window: if we should be flat, we shouldn't be opening
        if self.should_flatten_now():
            return False, "Lucid flatten window active (≥ LUCID_FLATTEN_TIME)"

        # 2. Economic release blackout
        near_rel, rel_reason = self.near_economic_release(CONFIG.lucid_econ_blackout_min)
        if near_rel:
            return False, f"Lucid econ blackout: {rel_reason}"

        # 3. EOD drawdown
        dd_ok, dd_reason = self.daily_drawdown_ok(state)
        if not dd_ok:
            return False, dd_reason

        # 4. Contract cap
        contr_ok, contr_reason = self.contracts_ok(sig.symbol, state)
        if not contr_ok:
            return False, contr_reason

        # 5. Microscalping guard: stop opening new trades once ≤5s winners
        #    make up too large a share of realized profit.
        scalp_ok, scalp_reason = self.scalp_profit_ok()
        if not scalp_ok:
            return False, scalp_reason

        return True, "ok"


# ── Convenience module-level functions (for callers that don't hold an instance) ──

def should_flatten_now() -> bool:
    """Module-level alias for quick one-off checks without an instance."""
    return LucidRiskManager().should_flatten_now()


def near_economic_release(blackout_min: int = 5) -> tuple[bool, str]:
    """Module-level alias for one-off checks."""
    return LucidRiskManager().near_economic_release(blackout_min)
