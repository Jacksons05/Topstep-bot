"""yfinance data enrichment — earnings calendar, IV rank, analyst consensus, news headlines.

All calls are cached for CONFIG.yf_cache_ttl_min (default: 15 min) to avoid rate limits.
The module degrades gracefully: if yfinance is not installed or any call fails, every
function returns a safe default so the rest of the pipeline continues unaffected.

IV Rank is approximated as:
    (current_atm_iv − min_52wk_rv) / (max_52wk_rv − min_52wk_rv) × 100
where min/max are computed from rolling 20-day realized volatility over the past year.
True IV rank requires a year of historical implied-vol data; the realised-vol proxy is a
reasonable substitute and keeps this module free of heavy dependencies.

Cache key schema
  earn:<SYM>     earnings-within-days check
  ivr:<SYM>      IV rank (float 0-100)
  analyst:<SYM>  analyst recommendation + target
  news:<SYM>     top-N yfinance headlines
"""
from __future__ import annotations

import contextlib
import io
import math
import os
import time
from datetime import date, datetime, timezone
from typing import Any

from config import CONFIG

# ── optional import ───────────────────────────────────────
try:
    import yfinance as yf
    _YF_OK = True
except ImportError:  # noqa: BLE001
    _YF_OK = False

# Indices and broad ETFs have no company fundamentals, so yfinance's
# quoteSummary endpoint returns "HTTP Error 404: No fundamentals data found"
# (printed to stderr) for every .info / earnings / analyst lookup. Skip them.
_NO_FUNDAMENTALS = {
    "SPX", "XSP", "NDX", "RUT", "VIX", "DJX",      # cash indices
    "SPY", "QQQ", "IWM", "DIA", "VOO", "IVV",      # broad ETFs
}

@contextlib.contextmanager
def _quiet():
    """Suppress yfinance's stderr chatter (e.g. "HTTP Error 404: No fundamentals
    data found for symbol: XLK") during .info lookups. Symbols outside
    _NO_FUNDAMENTALS — sector/thematic ETFs, foreign tickers — also lack a
    quoteSummary and would otherwise spam bot.err. Every caller fails open, so
    the dropped message carries no signal worth keeping."""
    devnull = open(os.devnull, "w")
    try:
        with contextlib.redirect_stderr(devnull), contextlib.redirect_stdout(io.StringIO()):
            yield
    finally:
        devnull.close()


# ── in-process TTL cache (monotonic, not wall-clock so pauses don't expire) ──
_CACHE: dict[str, tuple[float, Any]] = {}


def _ttl_sec() -> float:
    return float(CONFIG.yf_cache_ttl_min) * 60.0


def _get(key: str, fn, *args, **kwargs) -> Any:
    """Return the cached value if still fresh, else call fn(*args, **kwargs) and store it.

    On any exception the result is stored as None so a broken yfinance ticker doesn't
    hammer the API on every cycle — it will retry only after the TTL expires.
    """
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _ttl_sec():
        return hit[1]
    val: Any = None
    if _YF_OK and CONFIG.yfinance_enabled:
        try:
            val = fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            val = None
    _CACHE[key] = (now, val)
    return val


# ── pure-Python helpers (no numpy) ───────────────────────

def _rolling_ann_vol(closes: list[float], window: int = 20) -> list[float]:
    """Rolling annualized realized volatility from a list of daily closes.

    Returns one value per full window: standard deviation of log returns ×√252.
    Pure Python — no numpy — to avoid adding a heavy dep for this one calculation.
    """
    vols: list[float] = []
    for i in range(window, len(closes)):
        chunk = closes[i - window : i + 1]
        log_rets = [
            math.log(chunk[j] / chunk[j - 1])
            for j in range(1, len(chunk))
            if chunk[j - 1] > 0 and chunk[j] > 0
        ]
        if len(log_rets) < 2:
            continue
        mean = sum(log_rets) / len(log_rets)
        var = sum((r - mean) ** 2 for r in log_rets) / (len(log_rets) - 1)
        vols.append(math.sqrt(var) * math.sqrt(252))
    return vols


# ── public API ────────────────────────────────────────────

def earnings_within_days(symbol: str, days: int | None = None) -> tuple[bool, str]:
    """Return (True, reason) if the next earnings date is within `days` calendar days.

    Falls back to (False, '') when yfinance is disabled/unavailable or the date cannot
    be parsed — fail-open so a feed outage never freezes trading.
    """
    n = days if days is not None else CONFIG.skip_earnings_window_days
    result = _get(f"earn:{symbol}", _fetch_earnings_check, symbol, n)
    return result or (False, "")


def _fetch_earnings_check(symbol: str, days: int) -> tuple[bool, str]:
    if symbol.upper().lstrip("^") in _NO_FUNDAMENTALS:
        return False, ""          # indices/ETFs never report earnings
    ticker = yf.Ticker(symbol)
    earn_date: date | None = None

    # Newer yfinance: .calendar is a dict with key "Earnings Date".
    # Accessing it hits the network and 404s for ETFs/indices — keep quiet.
    with _quiet():
        cal = getattr(ticker, "calendar", None)
    if isinstance(cal, dict):
        raw = cal.get("Earnings Date")
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if raw is not None:
            if hasattr(raw, "date"):          # pandas Timestamp
                earn_date = raw.date()
            elif isinstance(raw, str):
                try:
                    earn_date = datetime.strptime(raw[:10], "%Y-%m-%d").date()
                except ValueError:
                    pass

    # Fallback: ticker.info carries earningsDate as a Unix timestamp in some versions
    if earn_date is None:
        with _quiet():
            info = ticker.info or {}
        raw = info.get("earningsDate") or info.get("earnings_date")
        if isinstance(raw, list) and raw:
            raw = raw[0]
        if isinstance(raw, (int, float)) and raw > 0:
            earn_date = datetime.fromtimestamp(raw, timezone.utc).date()

    if earn_date is None:
        return False, ""

    delta = (earn_date - date.today()).days
    if 0 <= delta <= days:
        return True, f"{symbol} earnings in {delta}d ({earn_date})"
    return False, ""


def iv_rank(symbol: str) -> float | None:
    """IV rank 0..100 (realized-vol proxy). Returns None when unavailable.

    Algorithm:
      1. Fetch the front-month ATM options chain and extract the ATM implied vol.
      2. Compute the 52-week range of 20-day rolling realised vol from price history.
      3. Rank current ATM IV against that range: (iv − min) / (max − min) × 100.

    Interpretation: >50 = elevated vol (consider selling), <30 = cheap vol (consider buying).
    """
    return _get(f"ivr:{symbol}", _fetch_iv_rank, symbol)


def _fetch_iv_rank(symbol: str) -> float | None:
    ticker = yf.Ticker(symbol)

    # ── 1. front-month ATM implied vol ───────────────────
    expiries = ticker.options
    if not expiries:
        return None

    # Fetch current spot from a short history window (5d is fast)
    spot_hist = ticker.history(period="5d")
    if spot_hist.empty:
        return None
    spot = float(spot_hist["Close"].iloc[-1])

    current_iv: float | None = None
    for exp in expiries[:3]:   # try up to 3 expiries in case the nearest is empty
        try:
            chain = ticker.option_chain(exp)
        except Exception:  # noqa: BLE001
            continue
        calls = getattr(chain, "calls", None)
        puts  = getattr(chain, "puts",  None)
        if calls is None or calls.empty:
            continue
        if "strike" not in calls.columns or "impliedVolatility" not in calls.columns:
            continue

        strikes = calls["strike"].values.tolist()
        if not strikes:
            continue
        atm = min(strikes, key=lambda k: abs(k - spot))

        ivs: list[float] = []
        for df in (calls, puts):
            if df is None or df.empty:
                continue
            row = df[df["strike"] == atm]
            if not row.empty:
                v = float(row["impliedVolatility"].iloc[0])
                if 0 < v < 10:       # sanity: yfinance returns decimals (0.25 = 25%), cap at 1000%
                    ivs.append(v)
        if ivs:
            current_iv = sum(ivs) / len(ivs)
            break

    if current_iv is None:
        return None

    # ── 2. 52-week realised vol range ─────────────────────
    hist_1y = ticker.history(period="1y")
    if len(hist_1y) < 25:
        return None

    closes = [float(c) for c in hist_1y["Close"].tolist()]
    vols = _rolling_ann_vol(closes, window=20)
    if not vols:
        return None

    lo, hi = min(vols), max(vols)
    if hi <= lo:
        return 50.0     # degenerate range → neutral

    # ── 3. rank ───────────────────────────────────────────
    rank = (current_iv - lo) / (hi - lo) * 100.0
    return max(0.0, min(100.0, round(rank, 1)))


def analyst_consensus(symbol: str) -> dict:
    """Return {'recommendation': str, 'target_price': float | None}.

    recommendation values: 'strong buy' | 'buy' | 'hold' | 'sell' | 'strong sell' | ''
    Returns empty defaults on any failure so callers don't need to guard.
    """
    result = _get(f"analyst:{symbol}", _fetch_analyst, symbol)
    return result or {"recommendation": "", "target_price": None}


# Normalise yfinance's raw recommendationKey strings to a human-readable label.
_REC_MAP: dict[str, str] = {
    "strongbuy":    "strong buy",
    "strong_buy":   "strong buy",
    "buy":          "buy",
    "outperform":   "buy",
    "overweight":   "buy",
    "hold":         "hold",
    "neutral":      "hold",
    "marketperform":  "hold",
    "market_perform": "hold",
    "equalweight":    "hold",
    "equal-weight":   "hold",
    "underperform":   "sell",
    "underweight":    "sell",
    "sell":           "sell",
    "strongsell":     "strong sell",
    "strong_sell":    "strong sell",
}


def _fetch_analyst(symbol: str) -> dict:
    if symbol.upper().lstrip("^") in _NO_FUNDAMENTALS:
        return {"recommendation": "", "target_price": None}   # no analyst coverage for indices/ETFs
    with _quiet():
        info = yf.Ticker(symbol).info or {}
    raw_rec = str(info.get("recommendationKey") or "").lower().replace(" ", "")
    rec = _REC_MAP.get(raw_rec, raw_rec[:20] if raw_rec else "")
    target = info.get("targetMeanPrice") or info.get("targetMedianPrice")
    return {
        "recommendation": rec,
        "target_price": float(target) if target else None,
    }


def news_headlines(symbol: str, n: int = 3) -> list[str]:
    """Return up to `n` recent headline strings from yfinance. Empty list on failure."""
    result = _get(f"news:{symbol}", _fetch_news, symbol, n)
    return result or []


def _fetch_news(symbol: str, n: int) -> list[str]:
    items = yf.Ticker(symbol).news or []
    out: list[str] = []
    for item in items:
        # Newer yfinance wraps article data under a nested "content" key
        title = (
            item.get("title")
            or (item.get("content") or {}).get("title")
            or ""
        ).strip()
        if title:
            out.append(title)
        if len(out) >= n:
            break
    return out


def clear_cache() -> None:
    """Flush all cached entries. Useful in tests or after a forced refresh."""
    _CACHE.clear()
