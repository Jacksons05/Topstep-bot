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

    def _macro_events(self) -> list[datetime]:
        frm, to = self._window()
        countries = {c.upper() for c in CONFIG.event_countries}
        def extract(j):
            rows = j.get("economicCalendar") or j.get("economic") or []
            out = []
            for e in rows:
                if str(e.get("impact", "")).lower() not in ("high", "3"):
                    continue
                # Only block on events for the configured countries (default US) — a
                # high-impact print in AU/CN/DE shouldn't freeze US-equity options.
                if countries and str(e.get("country", "")).upper() not in countries:
                    continue
                d = _parse_dt(e.get("time", "") or e.get("date", ""))
                if d:
                    out.append(d)
            return out
        return self._get(f"econ:{frm}", "/calendar/economic", {"from": frm, "to": to}, extract)

    def blackout(self, symbol: str, now: datetime | None = None) -> tuple[bool, str]:
        """(True, reason) if `symbol` is within the blackout window of an event."""
        if not self.enabled:
            return False, ""
        now = now or datetime.now(timezone.utc)
        win = timedelta(hours=CONFIG.event_blackout_hours)

        for d in self._earnings(symbol):
            # earnings is date-only -> blackout the whole day window around it
            if abs((d - now)) <= win + timedelta(days=1):
                return True, f"{symbol} earnings ~{d.date()}"

        for d in self._macro_events():
            # macro has a real event time -> tight symmetric window, no day buffer
            if abs((d - now)) <= win:
                return True, f"high-impact macro ~{d:%Y-%m-%d %H:%M}"

        return False, ""
