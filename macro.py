"""Macro regime feed (FRED) — fills the SymbolContext.macro slot the analyst reads.

Pulls a few slow-moving series (VIX, 10y, fed funds) and renders a one-line regime
note. Values change at most daily, so everything is cached for hours — FRED's free key
is generous but there's no reason to refetch each 60s cycle.

  VIXCLS — CBOE VIX (fear gauge / IV proxy for vol-edge)
  DGS10  — 10-year Treasury yield
  DFF    — effective fed funds rate

Best-effort: any failure degrades to an empty note / None VIX so a cycle never dies.
"""
from __future__ import annotations

import time

import httpx

from config import CONFIG

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
_TTL_SEC = 3600 * 4               # 4h — these series update daily at most
_cache: dict[str, tuple[float, float | None]] = {}


def _latest(series_id: str, http: httpx.Client) -> float | None:
    """Most-recent numeric observation for a FRED series, cached. None on any failure."""
    now = time.time()
    hit = _cache.get(series_id)
    if hit and now - hit[0] < _TTL_SEC:
        return hit[1]
    val: float | None = None
    try:
        r = http.get(FRED_BASE, params={
            "series_id": series_id,
            "api_key": CONFIG.fred_api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 1,
        })
        r.raise_for_status()
        obs = r.json().get("observations") or []
        if obs and obs[0].get("value") not in (None, "", "."):
            val = float(obs[0]["value"])
    except Exception:  # noqa: BLE001
        val = hit[1] if hit else None     # keep last good value on a blip
    _cache[series_id] = (now, val)
    return val


class Macro:
    """Cached FRED reads + a rendered regime line. Construct once per engine."""

    def __init__(self, timeout: float = 10.0):
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "jarvis/1.0"})

    def close(self) -> None:
        self._http.close()

    @property
    def enabled(self) -> bool:
        return CONFIG.macro_enabled and bool(CONFIG.fred_api_key)

    def vix(self) -> float | None:
        return _latest("VIXCLS", self._http) if self.enabled else None

    def line(self) -> str:
        """One-line macro note for the analyst prompt. Empty string when unavailable."""
        if not self.enabled:
            return ""
        vix = _latest("VIXCLS", self._http)
        ten = _latest("DGS10", self._http)
        ffr = _latest("DFF", self._http)
        bits = []
        if vix is not None:
            regime = "calm" if vix < 15 else ("elevated" if vix < 25 else "stressed")
            bits.append(f"VIX {vix:.1f} ({regime})")
        if ten is not None:
            bits.append(f"10y {ten:.2f}%")
        if ffr is not None:
            bits.append(f"FFR {ffr:.2f}%")
        return " · ".join(bits)
