"""Unusual Whales API integration — options flow + dark pool for futures proxies.

Maps futures roots to their liquid options proxies:
    ES / MES  →  SPX  (S&P 500 index options)
    NQ / MNQ  →  NDX  (Nasdaq-100 index options)

Fetches recent flow alerts and computes a directional lean from the net
call-vs-put premium ratio. Exposes the same interface the engine already
uses for order-flow data (fail-open on any error, TTL-cached).

Usage:
    feed = UWFlowFeed()
    read = feed.get("ES")          # UWFlowRead or None
    lines = feed.headlines("ES")   # list[str] for the news analyst
    feed.close()
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from config import CONFIG

# ── futures → UW ticker proxy ─────────────────────────────────────────────────
# Override via UW_PROXY_MAP in .env  e.g. "ES:SPX,NQ:NDX,MES:SPX,MNQ:NDX"
_DEFAULT_PROXY: dict[str, str] = {
    "ES": "SPX",
    "MES": "SPX",
    "NQ": "NDX",
    "MNQ": "NDX",
}

_UW_BASE = "https://api.unusualwhales.com"
_FLOW_PATH = "/api/option-contract/flow"
_TIDE_PATH = "/api/market/market-tide"

# UW API response field names — adjust here if the API changes shape.
_F_DATA = "data"
_F_TYPE = "type"          # "CALL" | "PUT"
_F_PREM = "total_premium" # float, dollars
_F_SIDE = "side"          # "ASK" (bought) | "BID" (sold to MM)
_F_EXPIRY = "expiry"
_F_STRIKE = "strike"
_F_TICKER = "ticker"
_F_VOLUME = "volume"


@dataclass
class UWFlowRead:
    lean: float               # -1..+1  (positive = net call premium dominant = bullish)
    strength: float           # abs(lean)
    whale: bool               # any single flow ticket ≥ UW_WHALE_PREMIUM_USD
    call_prem: float          # gross call premium in window ($)
    put_prem: float           # gross put premium in window ($)
    headlines: list[str] = field(default_factory=list)  # for news analyst
    ts: float = 0.0


def _proxy_map() -> dict[str, str]:
    """Build futures→UW ticker map, honoring UW_PROXY_MAP override."""
    raw = CONFIG.uw_proxy_map_raw
    if not raw:
        return _DEFAULT_PROXY.copy()
    out: dict[str, str] = {}
    for pair in raw.split(","):
        k, _, v = pair.partition(":")
        if k.strip() and v.strip():
            out[k.strip().upper()] = v.strip().upper()
    return out or _DEFAULT_PROXY.copy()


class UWFlowFeed:
    """Polls Unusual Whales flow + dark-pool endpoints with TTL caching."""

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=8.0,
            headers={
                "Authorization": f"Bearer {CONFIG.uw_api_key}",
                "User-Agent": "topstep-bot/1.0",
            },
            follow_redirects=True,
        )
        self._proxy = _proxy_map()
        self._cache: dict[str, tuple[float, UWFlowRead]] = {}  # proxy_ticker → (ts, read)
        self._ttl = CONFIG.uw_flow_cache_sec

    def close(self) -> None:
        self._http.close()

    # ── public interface ──────────────────────────────────────────────────────

    def get(self, futures_root: str) -> UWFlowRead | None:
        """Return UWFlowRead for *futures_root* (e.g. "ES"), or None on failure."""
        proxy = self._proxy.get(futures_root.upper())
        if not proxy:
            return None
        cached_ts, cached_read = self._cache.get(proxy, (0.0, None))  # type: ignore[assignment]
        if cached_read is not None and (time.time() - cached_ts) < self._ttl:
            return cached_read
        read = self._fetch(proxy)
        if read is not None:
            self._cache[proxy] = (time.time(), read)
        return read

    def headlines(self, futures_root: str) -> list[str]:
        """Return flow alerts as human-readable strings for the news analyst."""
        read = self.get(futures_root)
        return read.headlines if read else []

    # ── internals ─────────────────────────────────────────────────────────────

    def _fetch(self, proxy_ticker: str) -> UWFlowRead | None:
        try:
            resp = self._http.get(
                f"{_UW_BASE}{_FLOW_PATH}",
                params={"ticker": proxy_ticker, "limit": CONFIG.uw_flow_limit},
            )
            resp.raise_for_status()
            items = resp.json().get(_F_DATA, [])
            if not items:
                return None
            return self._parse(items, proxy_ticker)
        except Exception:  # noqa: BLE001
            return None

    def _parse(self, items: list[dict], proxy_ticker: str) -> UWFlowRead | None:
        call_prem = 0.0
        put_prem = 0.0
        whale = False
        top_tickets: list[dict] = []

        for item in items:
            prem = float(item.get(_F_PREM, 0) or 0)
            kind = str(item.get(_F_TYPE, "")).upper()
            if kind == "CALL":
                call_prem += prem
            elif kind == "PUT":
                put_prem += prem
            else:
                continue
            if prem >= CONFIG.uw_whale_premium_usd:
                whale = True
            # collect the top tickets by premium for headlines
            top_tickets.append(item)

        total = call_prem + put_prem
        if total == 0:
            return None

        lean = (call_prem - put_prem) / total  # -1..+1
        top_tickets.sort(key=lambda x: float(x.get(_F_PREM, 0) or 0), reverse=True)
        headlines = _build_headlines(top_tickets[:5], proxy_ticker)

        return UWFlowRead(
            lean=round(lean, 4),
            strength=round(abs(lean), 4),
            whale=whale,
            call_prem=call_prem,
            put_prem=put_prem,
            headlines=headlines,
            ts=time.time(),
        )


def _build_headlines(items: list[dict], proxy_ticker: str) -> list[str]:
    lines = []
    for item in items:
        kind = str(item.get(_F_TYPE, "")).upper()
        prem = float(item.get(_F_PREM, 0) or 0)
        expiry = item.get(_F_EXPIRY, "")
        strike = item.get(_F_STRIKE, "")
        side = item.get(_F_SIDE, "")
        sentiment = "bullish" if kind == "CALL" else "bearish"
        aggressor = "bought" if str(side).upper() == "ASK" else "sold"
        lines.append(
            f"[{sentiment}] Unusual {kind} flow {aggressor} on {proxy_ticker} "
            f"{strike} {expiry} — ${prem:,.0f} premium (UnusualWhales)"
        )
    return lines
