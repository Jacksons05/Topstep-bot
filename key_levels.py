"""Live key-levels context: prior-session reference levels plus today's
developing range/value-area, computed from bars the engine already fetches
each scan cycle.

PURE OBSERVABILITY / CONTEXT — this module does not gate, trigger, size, or
otherwise influence any entry. CONFIG.entry_engine controls trading logic
completely independently of anything here. The point is to make the bot's
own market-scanning level-aware (so it can be surfaced via notify/dashboard,
and later fed as context to the LLM confluence layer) without re-litigating
the mechanical level-reaction entry rules Rounds 24/25 already tested and
killed (value-area rotation, failed-auction fade) — this computes the SAME
kind of levels, it just doesn't trade a fixed rule off them.

Reuses research.features.value_area — the exact volume-profile bucketing
already frozen for Round 24 — so live and offline agree on what a POC/VAH/VAL
means.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from research.features import value_area

_ET = ZoneInfo("America/New_York")
_RTH_START_MIN = 9 * 60 + 30   # 09:30 ET
_RTH_END_MIN = 16 * 60         # 16:00 ET


@dataclass
class KeyLevels:
    """Reference levels for one symbol. Prior-session fields are settled and
    only change once per session; today_* fields update every cycle as more
    of the developing session prints."""
    session_date: date | None = None
    pdh: float | None = None
    pdl: float | None = None
    pdmid: float | None = None
    pdclose: float | None = None
    poc: float | None = None
    vah: float | None = None
    val: float | None = None
    today_hi: float | None = None
    today_lo: float | None = None
    today_poc: float | None = None
    today_vah: float | None = None
    today_val: float | None = None

    def summary(self) -> str:
        parts = []
        if self.pdh is not None:
            parts.append(f"PDH={self.pdh:.2f} PDL={self.pdl:.2f} PDmid={self.pdmid:.2f}")
        if self.poc is not None:
            parts.append(f"POC={self.poc:.2f} VAH={self.vah:.2f} VAL={self.val:.2f}")
        return " | ".join(parts) if parts else "no prior-session data yet"


def session_date_for(ts_et: datetime) -> date:
    """CME/Topstep session date — rolls at 18:00 ET. Mirrors
    Engine._topstep_session_date so both key off the identical boundary."""
    d = ts_et.date()
    if ts_et.hour >= 18:
        d = d + timedelta(days=1)
    return d


def _parse_bars_et(bars: dict):
    """Yield (ts_et, close, high, low, volume) oldest->newest, skipping any
    bar without a usable timestamp — nothing here works without one (matches
    _session_vwap's existing contract: bars lacking "time" are unusable for
    any session-anchored computation)."""
    times = bars.get("time") or []
    closes = bars.get("close") or []
    highs = bars.get("high") or []
    lows = bars.get("low") or []
    vols = bars.get("volume") or []
    n = min(len(times), len(closes), len(highs), len(lows), len(vols))
    for i in range(n):
        try:
            ts = datetime.fromisoformat(times[i]).astimezone(_ET)
        except (ValueError, TypeError):
            continue
        yield ts, float(closes[i]), float(highs[i]), float(lows[i]), float(vols[i])


def _rth_minute(ts: datetime) -> int:
    return ts.hour * 60 + ts.minute


def prior_session_levels(bars: dict) -> KeyLevels | None:
    """Prior RTH session's H/L/mid/close + volume-profile POC/VAH/VAL. Needs
    a bars pull wide enough to reach back through the full prior RTH session
    (78 5-min bars) plus the overnight session between it and today (~186
    5-min bars) — callers should pull a generous limit for this, separate
    from the smaller per-cycle pull used for `developing_levels`. None if the
    supplied bars don't reach back far enough to identify a prior session."""
    rows = list(_parse_bars_et(bars))
    if not rows:
        return None
    sessions: dict[date, list] = {}
    for ts, c, h, l, v in rows:
        if not (_RTH_START_MIN <= _rth_minute(ts) < _RTH_END_MIN):
            continue
        sessions.setdefault(session_date_for(ts), []).append((c, h, l, v))
    days = sorted(sessions)
    if len(days) < 2:
        return None  # can't identify a settled prior session
    prior_day = days[-2]
    seg = sessions[prior_day]
    closes = [r[0] for r in seg]
    highs = [r[1] for r in seg]
    lows = [r[2] for r in seg]
    vols = [r[3] for r in seg]
    kl = KeyLevels(session_date=prior_day, pdh=max(highs), pdl=min(lows),
                   pdmid=(max(highs) + min(lows)) / 2.0, pdclose=closes[-1])
    va = value_area(closes, vols)
    if va:
        kl.poc, kl.vah, kl.val = va
    return kl


def developing_levels(bars: dict, today: date):
    """(hi, lo, poc, vah, val) from TODAY's RTH bars only, so far — cheap to
    call every cycle since it reuses whatever bars the caller already has (no
    extra fetch). None if today has no RTH bars yet in the supplied window."""
    seg = [(c, h, l, v) for ts, c, h, l, v in _parse_bars_et(bars)
           if session_date_for(ts) == today and _RTH_START_MIN <= _rth_minute(ts) < _RTH_END_MIN]
    if not seg:
        return None
    closes = [r[0] for r in seg]
    highs = [r[1] for r in seg]
    lows = [r[2] for r in seg]
    vols = [r[3] for r in seg]
    va = value_area(closes, vols)
    poc, vah, val = va if va else (None, None, None)
    return max(highs), min(lows), poc, vah, val
