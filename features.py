"""Feature engineering for the ML quant signal.

One canonical feature vector, built the SAME way at train time (from historical
bars) and at live time (from the engine's bar buffer + the live order-flow
snapshot). Keeping a single builder is what makes backtest == live: train.py and
ml_signal.MLQuant both call `build_features` / `feature_row`.

Two feature families:

  * BAR features  — returns, trend, RSI, ATR%, realized vol, regime one-hot.
                    Always available (historical OHLCV is enough).
  * MICRO features — order-flow imbalance, CVD, micro-price skew, spread, whale,
                     CVD divergence. Only present LIVE (or once you've recorded
                     L2 with datafeed.LiveRecorder). When absent they are NaN —
                     LightGBM handles NaN natively, so a model bootstrapped on
                     bar-only history still runs, and a model retrained on
                     recorded L2 history picks the micro columns up automatically.

`FEATURE_NAMES` is the ordered contract. Persist it next to the model; never
reorder (LightGBM keys on position). Append-only if you add features.
"""
from __future__ import annotations

import numpy as np

from config import CONFIG
from regime import classify_last
from signals import atr as _atr_list, rsi as _rsi_list, sma as _sma_list

# ── ordered feature contract ──────────────────────────────────────────────
_BAR_FEATURES: tuple[str, ...] = (
    "ret_1", "ret_5", "ret_10", "ret_20",
    "rvol_10", "rvol_20",
    "sma_ratio",        # (sma_fast - sma_slow) / sma_slow   trend sign+strength
    "px_vs_sma_fast",   # (close - sma_fast) / sma_fast       stretch from trend
    "rsi",              # 0..100
    "atr_pct",          # atr / price                          normalized vol
    "range_pct",        # (high - low) / close   last bar      bar range
    "mom_accel",        # ret_5 - ret_10                        momentum accel
    "regime_trending", "regime_meanrev", "regime_consol", "regime_crisis",
)
_MICRO_FEATURES: tuple[str, ...] = (
    "obi",          # -1..+1 order-book imbalance
    "cvd",          # cumulative volume delta (since session reset)
    "micro_skew",   # (micro_price - mid) / mid
    "spread_pct",   # (ask - bid) / mid
    "whale",        # -1 / 0 / +1
    "cvd_div",      # -1 bearish / 0 / +1 bullish
)
FEATURE_NAMES: tuple[str, ...] = _BAR_FEATURES + _MICRO_FEATURES

_REGIME_INDEX = {
    "Trending": "regime_trending",
    "Mean-Reversion": "regime_meanrev",
    "Consolidation": "regime_consol",
    "Crisis": "regime_crisis",
}


def _log_ret(closes: list[float], n: int) -> float:
    """Log return over the last n bars; NaN when not enough history or a
    non-positive price would break the log."""
    if len(closes) <= n:
        return float("nan")
    a, b = closes[-1 - n], closes[-1]
    if a <= 0 or b <= 0:
        return float("nan")
    return float(np.log(b / a))


def _realized_vol(closes: list[float], n: int) -> float:
    """Std of the last n one-bar log returns (NaN when too short)."""
    if len(closes) < n + 1:
        return float("nan")
    seg = np.asarray(closes[-n - 1:], dtype=np.float64)
    if np.any(seg <= 0):
        return float("nan")
    rets = np.diff(np.log(seg))
    return float(np.std(rets))


def _micro_row(micro: dict | None) -> dict[str, float]:
    """Microstructure feature slice. All NaN when no live order-flow snapshot,
    so historical bar-only training leaves these columns empty (ignored)."""
    if not micro:
        return {k: float("nan") for k in _MICRO_FEATURES}
    bid, ask = micro.get("bid", 0.0), micro.get("ask", 0.0)
    mid = (bid + ask) / 2 if (bid and ask) else micro.get("micro_price", 0.0)
    mp = micro.get("micro_price", mid)
    div = micro.get("cvd_div", "")
    div_code = 1.0 if div == "bullish" else (-1.0 if div == "bearish" else 0.0)
    return {
        "obi": float(micro.get("obi", float("nan"))),
        "cvd": float(micro.get("cvd", float("nan"))),
        "micro_skew": float((mp - mid) / mid) if mid else float("nan"),
        "spread_pct": float((ask - bid) / mid) if (mid and bid and ask) else float("nan"),
        "whale": float(micro.get("whale", 0.0)),
        "cvd_div": div_code,
    }


def feature_row(bars: dict, micro: dict | None = None) -> dict[str, float] | None:
    """Build ONE named feature row from a bar buffer (oldest→newest) and an
    optional live order-flow dict. Returns None when there aren't enough bars to
    compute the slow SMA / longest lookback. `micro` keys (all optional):
    obi, cvd, micro_price, bid, ask, whale, cvd_div."""
    closes = bars.get("close") or []
    highs = bars.get("high") or closes
    lows = bars.get("low") or closes
    need = max(CONFIG.sma_slow + 1, 21)
    if len(closes) < need:
        return None

    sma_f = _sma_list(closes, CONFIG.sma_fast)
    sma_s = _sma_list(closes, CONFIG.sma_slow)
    r = _rsi_list(closes, CONFIG.rsi_period)
    a = _atr_list(highs, lows, closes, CONFIG.atr_period) or 0.0
    px = closes[-1]
    if sma_f is None or sma_s is None or r is None or sma_s <= 0 or px <= 0:
        return None

    ret_5, ret_10 = _log_ret(closes, 5), _log_ret(closes, 10)
    row: dict[str, float] = {
        "ret_1": _log_ret(closes, 1),
        "ret_5": ret_5,
        "ret_10": ret_10,
        "ret_20": _log_ret(closes, 20),
        "rvol_10": _realized_vol(closes, 10),
        "rvol_20": _realized_vol(closes, 20),
        "sma_ratio": (sma_f - sma_s) / sma_s,
        "px_vs_sma_fast": (px - sma_f) / sma_f if sma_f else float("nan"),
        "rsi": r,
        "atr_pct": a / px,
        "range_pct": (highs[-1] - lows[-1]) / px,
        "mom_accel": (ret_5 - ret_10) if not (np.isnan(ret_5) or np.isnan(ret_10)) else float("nan"),
        "regime_trending": 0.0, "regime_meanrev": 0.0,
        "regime_consol": 0.0, "regime_crisis": 0.0,
    }
    label = classify_last(closes, highs, lows)
    row[_REGIME_INDEX.get(label, "regime_meanrev")] = 1.0
    row.update(_micro_row(micro))
    return row


def row_to_vector(row: dict[str, float]) -> np.ndarray:
    """Ordered float32 vector in FEATURE_NAMES order — the model's input."""
    return np.array([row.get(k, float("nan")) for k in FEATURE_NAMES], dtype=np.float32)


def build_features(closes, highs, lows) -> tuple[np.ndarray, list[int]]:
    """Vectorized-ish historical matrix for training (bar features only; micro
    columns are NaN — see datafeed.LiveRecorder to add real L2 history).

    Returns (X, idx) where X[i] is the feature vector computed using bars
    0..idx[i] inclusive, so X[i] is causal (no lookahead). idx maps each row
    back to its bar index for label alignment in train.py.
    """
    closes = list(map(float, closes))
    highs = list(map(float, highs))
    lows = list(map(float, lows))
    n = len(closes)
    need = max(CONFIG.sma_slow + 1, 21)
    rows: list[np.ndarray] = []
    idx: list[int] = []
    for i in range(need - 1, n):
        bars = {"close": closes[: i + 1], "high": highs[: i + 1], "low": lows[: i + 1]}
        row = feature_row(bars)
        if row is None:
            continue
        rows.append(row_to_vector(row))
        idx.append(i)
    X = np.vstack(rows) if rows else np.empty((0, len(FEATURE_NAMES)), dtype=np.float32)
    return X, idx
