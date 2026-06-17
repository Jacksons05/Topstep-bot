"""Market-regime classification — shared by the live engine and the backtest.

A trade's regime is read from its entry-bar structure:

    vol = ATR / price          (realized-vol proxy)
    ts  = |SMA_fast - SMA_slow| / SMA_slow   (trend strength)

Buckets (thresholds are data-driven quantiles over the supplied window, so they
adapt per symbol / timeframe):

    Crisis        — vol in the top decile (panic/high-vol entries)
    Trending      — strong trend, not crisis
    Consolidation — low vol AND weak trend
    Mean-Reversion— everything else

Numpy-only on purpose: the live engine imports `classify_last` and must not pull
in numba/polars. The backtest uses `regime_labels` for the full series.
"""
from __future__ import annotations

import numpy as np

from config import CONFIG

REGIMES = ("Trending", "Mean-Reversion", "Consolidation", "Crisis")


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    pc = closes[:-1]
    tr[1:] = np.maximum.reduce([highs[1:] - lows[1:],
                                np.abs(highs[1:] - pc),
                                np.abs(lows[1:] - pc)])
    c = np.cumsum(np.insert(tr, 0, 0.0))
    out[period - 1:] = (c[period:] - c[:-period]) / period
    return out


def regime_labels(closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> np.ndarray:
    """Per-bar regime label array (object dtype). Bars too early to classify
    default to 'Mean-Reversion'."""
    closes = np.asarray(closes, dtype=np.float64)
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)
    n = len(closes)
    labels = np.array(["Mean-Reversion"] * n, dtype=object)
    if n < CONFIG.sma_slow + 2:
        return labels

    fast = _sma(closes, CONFIG.sma_fast)
    slow = _sma(closes, CONFIG.sma_slow)
    a = _atr(highs, lows, closes, CONFIG.atr_period)
    price = np.where(closes > 0, closes, np.nan)
    vol = a / price
    ts = np.where(slow > 0, np.abs(fast - slow) / slow, np.nan)

    fv = vol[np.isfinite(vol)]
    ft = ts[np.isfinite(ts)]
    if fv.size == 0:
        return labels
    vol_hi = np.quantile(fv, 0.90)
    vol_lo = np.quantile(fv, 0.33)
    ts_hi = np.quantile(ft, 0.66) if ft.size else 0.0
    ts_lo = np.quantile(ft, 0.33) if ft.size else 0.0

    for i in range(n):
        v = vol[i] if np.isfinite(vol[i]) else 0.0
        t = ts[i] if np.isfinite(ts[i]) else 0.0
        if v == 0.0 and not np.isfinite(vol[i]):
            continue
        if v >= vol_hi:
            labels[i] = "Crisis"
        elif t >= ts_hi:
            labels[i] = "Trending"
        elif v <= vol_lo and t <= ts_lo:
            labels[i] = "Consolidation"
        else:
            labels[i] = "Mean-Reversion"
    return labels


def classify_last(closes, highs, lows) -> str:
    """Regime of the most recent bar — the live engine's per-symbol read."""
    labels = regime_labels(closes, highs, lows)
    return str(labels[-1]) if len(labels) else "Mean-Reversion"
