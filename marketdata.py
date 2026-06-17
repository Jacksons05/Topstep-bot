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

    def bars(self, symbol: str, timeframe: str = "1Day", limit: int = 120) -> dict:
        """Return {"close":[...], "high":[...], "low":[...]} oldest->newest.

        Empty dict on any failure. `timeframe` follows Alpaca syntax
        (1Min, 5Min, 15Min, 1Hour, 1Day).
        """
        # Index underlyings (SPX/XSP) have no Alpaca stocks feed — read direction off
        # the tracking ETF (SPX->SPY). Price/GEX still come from the real index chain.
        if CONFIG.is_index(symbol):
            symbol = CONFIG.proxy_for(symbol)
        start = (datetime.now(timezone.utc) - timedelta(days=limit * 2 + 5)).date().isoformat()
        try:
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
        except Exception:  # noqa: BLE001
            return {}
        data = list(reversed(data))  # back to oldest->newest for the indicators
        return {
            "close": [float(b["c"]) for b in data],
            "high": [float(b["h"]) for b in data],
            "low": [float(b["l"]) for b in data],
            "volume": [float(b.get("v", 0)) for b in data],
        }

    def quote(self, symbol: str) -> Quote | None:
        # Index spot comes from the real CBOE index chain (Alpaca has no index feed),
        # so every downstream price (spot/stop/target/exit) stays at index scale.
        if CONFIG.is_index(symbol):
            from options import cboe_chain
            chain = cboe_chain(symbol, 0.0)
            if chain is None or chain.spot <= 0:
                return None
            return Quote(symbol=symbol, price=float(chain.spot),
                         ts=datetime.now(timezone.utc).isoformat())
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

    def intraday_change_pct(self, symbol: str) -> float:
        """Today's % move vs the prior close — powers the circuit breaker."""
        b = self.bars(symbol, timeframe="1Day", limit=2)
        closes = b.get("close") or []
        if len(closes) < 2 or closes[-2] == 0:
            return 0.0
        return (closes[-1] - closes[-2]) / closes[-2] * 100.0
