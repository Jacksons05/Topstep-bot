"""Unusual Whales dealer-GEX regime feed (Phase 4 strategy pivot).

Polls /api/stock/{proxy}/greek-exposure for the futures symbol's options proxy
(MES→SPY, MNQ→QQQ, …) and classifies the day's dealer net-gamma regime:

    positive  net GEX ≥ +band   dealers long gamma → vol suppressed →
                                VWAP mean-reversion entries
    negative  net GEX ≤ −band   dealers short gamma → vol expanded →
                                reduced-risk breakout/momentum entries
    neutral   |net GEX| < band  no reliable dealer-hedging pressure →
                                entries LOCKED (choppy, low-EV)

Sign convention matches the sister repo's uw_history.greek_exposure_daily and
the SqueezeMetrics series used in research/gamma_rv_precheck.py: net_gamma =
call_gamma + put_gamma, > 0 = dealers long gamma (vol-suppressing).

The neutral band is self-normalizing: band = GEX_NEUTRAL_BAND_FRAC × median
|net_gamma| over the trailing rows the endpoint returns, so no hand-tuned
dollar threshold that rots as OI regimes change.

FAIL-CLOSED: any fetch/parse failure (or a missing API key) yields regime
"neutral", which locks new entries. A dead GEX feed can never default the
engine into trading.

RESEARCH STATUS (honesty note): this repo's own OOS Round 18 (dealer net-gamma
*reversal* at daily frequency) FAILED, and Round 1/19 killed the SMA/RSI
entries this replaces. The regime *toggle* implemented here (vol-regime
switching, not reversal) matches the JARVIS-side empirical pre-check that DID
survive its controls — but it has NOT itself passed this repo's OOS harness.
Treat live enablement as an experiment: kill switch off only with sim/eval.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass

import httpx

from config import CONFIG

log = logging.getLogger(__name__)

_UW_BASE = "https://api.unusualwhales.com"

# Futures root → UW options proxy with a liquid listed chain.
_DEFAULT_GEX_PROXY: dict[str, str] = {
    "ES": "SPY", "MES": "SPY",
    "NQ": "QQQ", "MNQ": "QQQ",
    "YM": "DIA", "MYM": "DIA",
    "RTY": "IWM", "M2K": "IWM",
    "GC": "GLD", "MGC": "GLD",
    "CL": "USO", "MCL": "USO",
}


def _f(x) -> float:
    try:
        v = float(x)
        return v if v == v else 0.0  # NaN → 0
    except (TypeError, ValueError):
        return 0.0


def gex_proxy_map() -> dict[str, str]:
    """Futures→UW GEX proxy map, honoring the UW_GEX_PROXY_MAP override."""
    raw = CONFIG.uw_gex_proxy_map_raw
    if not raw:
        return _DEFAULT_GEX_PROXY.copy()
    out: dict[str, str] = {}
    for pair in raw.split(","):
        k, _, v = pair.partition(":")
        if k.strip() and v.strip():
            out[k.strip().upper()] = v.strip().upper()
    return out or _DEFAULT_GEX_PROXY.copy()


def classify_gex(net_gamma: float, history_abs: list[float],
                 band_frac: float) -> str:
    """Classify a net-gamma reading into positive / negative / neutral.

    band = band_frac × median(|net_gamma| history). An empty/degenerate
    history (all zeros) leaves the band at 0 — then ANY nonzero reading
    classifies by sign, and an exactly-zero reading is neutral. A zero
    reading with a real band is always neutral.
    """
    vals = [abs(v) for v in history_abs if v == v and abs(v) > 0]
    band = band_frac * statistics.median(vals) if vals else 0.0
    if net_gamma > band:
        return "positive"
    if net_gamma < -band:
        return "negative"
    return "neutral"


@dataclass
class GexRead:
    regime: str          # "positive" | "negative" | "neutral"
    net_gamma: float     # today's dealer net gamma (proxy underlying)
    band: float          # neutral half-band actually applied
    proxy: str           # UW ticker the read came from
    ts: float = 0.0


class UWGexFeed:
    """TTL-cached dealer net-GEX regime per futures symbol (via options proxy).

    greek-exposure is DAILY data — one row per session, today's row built from
    live OI/greeks — so the default 15-min TTL is generous, not stale.
    """

    def __init__(self) -> None:
        self._http = httpx.Client(
            timeout=8.0,
            headers={
                "Authorization": f"Bearer {CONFIG.uw_api_key}",
                "User-Agent": "topstep-bot/1.0",
            },
            follow_redirects=True,
        )
        self._proxy = gex_proxy_map()
        self._cache: dict[str, tuple[float, GexRead]] = {}  # proxy → (ts, read)

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass

    def get(self, symbol: str) -> GexRead:
        """Regime read for a futures symbol. NEVER raises: any failure returns
        a neutral read (entries locked) — the fail-closed default."""
        proxy = self._proxy.get(symbol.upper())
        if proxy is None:
            return GexRead("neutral", 0.0, 0.0, proxy="", ts=time.time())
        now = time.time()
        hit = self._cache.get(proxy)
        if hit and now - hit[0] < CONFIG.uw_gex_cache_sec:
            return hit[1]
        read = self._fetch(proxy)
        # Cache failures too (as neutral) so a down API isn't hammered every scan.
        self._cache[proxy] = (now, read)
        return read

    def _fetch(self, proxy: str) -> GexRead:
        try:
            r = self._http.get(f"{_UW_BASE}/api/stock/{proxy}/greek-exposure")
            r.raise_for_status()
            rows = r.json().get("data") or []
            if not rows:
                raise ValueError("empty greek-exposure response")
            # Rows are dated ascending or descending depending on endpoint
            # version — sort by date so [-1] is always the latest session.
            rows = sorted(rows, key=lambda x: str(x.get("date") or ""))
            nets = [_f(x.get("call_gamma")) + _f(x.get("put_gamma")) for x in rows]
            net = nets[-1]
            regime = classify_gex(net, nets[:-1] or nets,
                                  CONFIG.gex_neutral_band_frac)
            vals = [abs(v) for v in (nets[:-1] or nets) if abs(v) > 0]
            band = (CONFIG.gex_neutral_band_frac * statistics.median(vals)
                    if vals else 0.0)
            return GexRead(regime, net, band, proxy=proxy, ts=time.time())
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[UW-GEX] {proxy} fetch failed ({exc}) — "
                        "regime=neutral (fail closed, entries locked)")
            return GexRead("neutral", 0.0, 0.0, proxy=proxy, ts=time.time())
