"""Market data feed — OHLC bars + latest quote, via Alpaca's data API.

Best-effort: any network hiccup degrades to None/empty so a cycle never dies
on a feed. Uses Alpaca's free IEX feed; swap to SIP with a paid plan by setting
DATA_FEED=sip in the env (passed through to the `feed` query param).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from config import CONFIG
from futures_symbols import is_futures_symbol

_DATA_FEED = os.getenv("DATA_FEED", "iex")


@dataclass
class Quote:
    symbol: str
    price: float
    ts: str


class MarketData:
    def __init__(self, timeout: float = 10.0):
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "APCA-API-KEY-ID": CONFIG.alpaca_api_key,
                "APCA-API-SECRET-KEY": CONFIG.alpaca_secret_key,
                "User-Agent": "jarvis-stock/1.0",
            },
        )

    def close(self) -> None:
        self._http.close()

    def _fetch_bars(self, symbol: str, timeframe: str, limit: int) -> dict:
        """Fetch bars, RAISING on any transport/HTTP error so callers can tell a
        data OUTAGE apart from a genuine empty/flat result. See bars() for the
        error-swallowing wrapper used where {} is an acceptable degrade."""
        if CONFIG.is_index(symbol):
            symbol = CONFIG.proxy_for(symbol)
        start = (datetime.now(timezone.utc) - timedelta(days=limit * 2 + 5)).date().isoformat()
        r = self._http.get(
            f"{CONFIG.alpaca_data_url}/v2/stocks/{symbol}/bars",
            params={
                "timeframe": timeframe,
                "start": start,
                "limit": limit,
                "adjustment": "split",
                "feed": _DATA_FEED,
                # newest-first so `limit` returns the MOST RECENT bars, not the oldest
                # in the window (which left intraday timeframes on months-stale data).
                "sort": "desc",
            },
        )
        r.raise_for_status()
        data = r.json().get("bars") or []
        data = list(reversed(data))  # back to oldest->newest for the indicators
        return {
            "close": [float(b["c"]) for b in data],
            "high": [float(b["h"]) for b in data],
            "low": [float(b["l"]) for b in data],
            "volume": [float(b.get("v", 0)) for b in data],
        }

    def bars(self, symbol: str, timeframe: str = "1Day", limit: int = 120) -> dict:
        """Return {"close":[...], "high":[...], "low":[...]} oldest->newest.

        Empty dict on any failure. `timeframe` follows Alpaca syntax
        (1Min, 5Min, 15Min, 1Hour, 1Day).
        """
        try:
            return self._fetch_bars(symbol, timeframe, limit)
        except Exception:  # noqa: BLE001
            return {}

    def quote(self, symbol: str) -> Quote | None:
        # A futures root must NEVER be priced off the Alpaca equities endpoint:
        # roots collide with unrelated stock tickers (ES=Eversource ~$60,
        # GC/CL/SI/NQ/YM/ZB/ZN too), so a "quote" here would be a phantom mark
        # feeding the Topstep equity/breach math -> spurious trailing-MLL breach
        # and wrongful liquidation. Futures marks come only from the ProjectX
        # flow feed / BBO (see engine._mark). Callers already handle None.
        if is_futures_symbol(symbol):
            return None
        try:
            r = self._http.get(
                f"{CONFIG.alpaca_data_url}/v2/stocks/{symbol}/trades/latest",
                params={"feed": _DATA_FEED},
            )
            r.raise_for_status()
            t = r.json().get("trade") or {}
        except Exception:  # noqa: BLE001
            return None
        if "p" not in t:
            return None
        return Quote(symbol=symbol, price=float(t["p"]), ts=str(t.get("t", "")))

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for s in symbols:
            q = self.quote(s)
            if q:
                out[s] = q
        return out

    def intraday_change_pct(self, symbol: str) -> float | None:
        """Today's % move vs the prior close — powers the circuit breaker.

        Returns None on a DATA OUTAGE or insufficient history (caller must fail
        CLOSED — halt new entries), distinct from a genuine 0.0% move (flat tape).

        Source order: Alpaca (if keyed) -> Unusual Whales stock-state -> yfinance.
        The result is cached ~60s: the %move vs prior close barely changes intra-
        minute, and polling it every scan otherwise exhausts the UW daily request
        quota (a silent 429 that pins the circuit breaker RED for the rest of the
        day). Caching also spares the free yfinance fallback from per-scan calls.
        """
        import time as _t
        cache = self.__dict__.setdefault("_regime_cache", {})
        now = _t.time()
        hit = cache.get(symbol)
        if hit is not None and now < hit[0]:
            return hit[1]
        val = self._compute_change_pct(symbol)
        # Cache successes 60s; failures 15s so a dead source is not hammered every
        # scan yet recovery is still picked up quickly.
        cache[symbol] = (now + (60.0 if val is not None else 15.0), val)
        return val

    def _compute_change_pct(self, symbol: str) -> float | None:
        if CONFIG.alpaca_api_key:
            try:
                b = self._fetch_bars(symbol, "1Day", 2)
                closes = b.get("close") or []
                if len(closes) >= 2 and closes[-2] != 0:
                    return (closes[-1] - closes[-2]) / closes[-2] * 100.0
            except Exception:  # noqa: BLE001
                pass  # fall through to the keyless fallbacks
        v = self._intraday_change_pct_uw(symbol)
        if v is not None:
            return v
        return self._intraday_change_pct_yf(symbol)

    def _intraday_change_pct_yf(self, symbol: str) -> float | None:
        """Free yfinance fallback (no key) for the regime %-change. Used when
        Alpaca is unkeyed and the UW stock-state endpoint is unavailable (e.g.
        the daily request quota is hit and every call 429s)."""
        try:
            import yfinance as yf
            h = yf.Ticker(symbol).history(period="2d")
            closes = list(h["Close"]) if not h.empty else []
        except Exception:  # noqa: BLE001
            return None
        if len(closes) < 2 or not closes[-2]:
            return None
        return (closes[-1] - closes[-2]) / closes[-2] * 100.0

    def _intraday_change_pct_uw(self, symbol: str) -> float | None:
        """Unusual Whales fallback for intraday_change_pct — see there for contract."""
        if not CONFIG.uw_api_key:
            return None
        try:
            r = httpx.get(
                f"https://api.unusualwhales.com/api/stock/{symbol}/stock-state",
                headers={"Authorization": f"Bearer {CONFIG.uw_api_key}"},
                timeout=10.0,
            )
            r.raise_for_status()
            d = r.json().get("data") or {}
            close, prev_close = float(d["close"]), float(d["prev_close"])
        except Exception:  # noqa: BLE001
            return None  # transport/HTTP/parse failure → unknown, caller fails closed
        if prev_close == 0:
            return None
        return (close - prev_close) / prev_close * 100.0
