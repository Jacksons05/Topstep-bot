"""Event-risk blackout (Finnhub calendars) — keeps the bot from opening into a print.

The strategy + research stress that 0DTE/dealer-gamma behavior goes haywire around
high-impact macro prints (CPI, FOMC, NFP) and single-name earnings (GEX restructures).
This module answers one question for the risk gate: "is `symbol` inside a blackout window
right now?" — i.e. within EVENT_BLACKOUT_HOURS of its earnings, or of any high-impact
macro event.

Finnhub free tier: earnings calendar is available; the economic calendar may be premium
on some plans, so a 403/empty there degrades silently (earnings blackout still works).

Calendars change at most daily -> cached for hours. Best-effort: failures => no blackout
(fail-open, so a feed outage never freezes trading; the other risk layers still apply).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx

from config import CONFIG

FINNHUB_BASE = "https://finnhub.io/api/v1"
_TTL_SEC = 3600 * 6
# cache: key -> (fetched_ts, list[datetime] of event times in UTC)
_cache: dict[str, tuple[float, list[datetime]]] = {}


def _parse_day(s: str) -> datetime | None:
    """Finnhub dates are 'YYYY-MM-DD' (treat as that day, 00:00 UTC)."""
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _parse_dt(s: str) -> datetime | None:
    """Parse Finnhub economic 'time' = 'YYYY-MM-DD HH:MM:SS' (real event time, UTC).
    Falls back to the date if no time component."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return _parse_day(s)


class Events:
    def __init__(self, timeout: float = 10.0):
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "jarvis/1.0"})

    def close(self) -> None:
        self._http.close()

    @property
    def enabled(self) -> bool:
        return CONFIG.event_blackout_enabled and bool(CONFIG.finnhub_api_key)

    def _window(self) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        h = CONFIG.event_blackout_hours
        return ((now - timedelta(hours=h)).date().isoformat(),
                (now + timedelta(hours=h)).date().isoformat())

    def _get(self, key: str, path: str, params: dict, extract) -> list[datetime]:
        now = time.time()
        hit = _cache.get(key)
        if hit and now - hit[0] < _TTL_SEC:
            return hit[1]
        out: list[datetime] = []
        try:
            r = self._http.get(f"{FINNHUB_BASE}{path}",
                               params={**params, "token": CONFIG.finnhub_api_key})
            r.raise_for_status()
            out = extract(r.json())
        except Exception:  # noqa: BLE001
            out = hit[1] if hit else []
        _cache[key] = (now, out)
        return out

    def _earnings(self, symbol: str) -> list[datetime]:
        frm, to = self._window()
        return self._get(
            f"earn:{symbol}:{frm}", "/calendar/earnings",
            {"from": frm, "to": to, "symbol": symbol},
            lambda j: [d for e in (j.get("earningsCalendar") or [])
                       if (d := _parse_day(e.get("date", ""))) is not None],
        )

    # Per-event-type asymmetric blackout windows (minutes before/after release).
    # Research: FOMC spike resolves in ~60 min; CPI/NFP in ~30 min.
    # Key: lowercase substring match against the Finnhub event name.
    _EVENT_WINDOWS: list[tuple[str, int, int]] = [
        # (name_substr, pre_min, post_min)
        ("fomc",              30, 60),
        ("federal",           30, 60),   # "Federal Open Market"
        ("interest rate",     30, 60),
        ("cpi",               15, 30),
        ("consumer price",    15, 30),
        ("nonfarm",           15, 30),
        ("nfp",               15, 30),
        ("ppi",               15, 30),
        ("producer price",    15, 30),
        ("gdp",               10, 20),
        ("pce",               10, 20),
        ("core pce",          10, 20),
        ("retail sales",      10, 15),
    ]
    _DEFAULT_PRE_MIN  = 30    # fallback pre-window for unlisted high-impact events
    _DEFAULT_POST_MIN = 60

    def _macro_events(self) -> list[tuple[datetime, str]]:
        """Returns list of (event_time_utc, event_name) for upcoming high-impact prints."""
        frm, to = self._window()
        countries = {c.upper() for c in CONFIG.event_countries}
        def extract(j):
            rows = j.get("economicCalendar") or j.get("economic") or []
            out = []
            for e in rows:
                if str(e.get("impact", "")).lower() not in ("high", "3"):
                    continue
                if countries and str(e.get("country", "")).upper() not in countries:
                    continue
                d = _parse_dt(e.get("time", "") or e.get("date", ""))
                if d:
                    name = str(e.get("event", "")).strip()
                    out.append((d, name))
            return out
        return self._get(f"econ2:{frm}", "/calendar/economic", {"from": frm, "to": to}, extract)  # type: ignore[return-value]

    def _event_window(self, name: str) -> tuple[timedelta, timedelta]:
        """Return (pre_blackout, post_blackout) for an event name."""
        n = name.lower()
        for substr, pre, post in self._EVENT_WINDOWS:
            if substr in n:
                return timedelta(minutes=pre), timedelta(minutes=post)
        return timedelta(minutes=self._DEFAULT_PRE_MIN), timedelta(minutes=self._DEFAULT_POST_MIN)

    def blackout(self, symbol: str, now: datetime | None = None) -> tuple[bool, str]:
        """(True, reason) if `symbol` is within the blackout window of an event."""
        if not self.enabled:
            return False, ""
        now = now or datetime.now(timezone.utc)
        earn_win = timedelta(hours=CONFIG.event_blackout_hours)

        for d in self._earnings(symbol):
            if abs((d - now)) <= earn_win + timedelta(days=1):
                return True, f"{symbol} earnings ~{d.date()}"

        for d, name in self._macro_events():
            pre, post = self._event_window(name)
            delta = now - d   # positive = we're AFTER the event
            if -pre <= delta <= post:
                label = name or "high-impact macro"
                return True, f"{label} ~{d:%Y-%m-%d %H:%M}"

        return False, ""
