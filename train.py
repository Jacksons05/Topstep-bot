"""Train the LightGBM quant signal with purged, embargoed walk-forward CV.

Pipeline (the four upgrades, end to end):
    datafeed.load_history → features.build_features → mlcv.triple_barrier_labels
    → mlcv.PurgedKFold (leakage-safe eval) → LightGBM → models/quant_lgbm.txt

Usage:
    # bootstrap from a FirstRateData / OHLCV export (bar-only features):
    python train.py --symbol ES --csv data/ES_5min.csv
    # retrain on your own recorded ProjectX feed (adds live L2 micro features):
    python train.py --symbol ES

The CV scores are what you trust — NOT the in-sample fit. A model that scores
~0.5 AUC out-of-fold has no edge; do not deploy it. Set ML_SIGNAL_ENABLED=true
only after the purged CV AUC is convincingly > 0.5 and stable across folds.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import numpy as np

from config import CONFIG
from datafeed import load_history
from features import FEATURE_NAMES, build_features
from mlcv import PurgedKFold, triple_barrier_labels
from regime import _atr as _atr_series  # vectorized ATR over the full series

log = logging.getLogger("train")
ROOT = Path(__file__).resolve().parent


def _labels_for(bars: dict, idx: list[int], horizon: int) -> tuple[np.ndarray, np.ndarray]:
    """Triple-barrier labels aligned to the feature-row bar indices `idx`."""
    closes = np.asarray(bars["close"], dtype=np.float64)
    highs = np.asarray(bars.get("high") or bars["close"], dtype=np.float64)
    lows = np.asarray(bars.get("low") or bars["close"], dtype=np.float64)
    atr = _atr_series(highs, lows, closes, CONFIG.atr_period)
    y_all, w_all = triple_barrier_labels(
        closes, highs, lows, atr,
        horizon=horizon, up_mult=CONFIG.ml_label_up_atr, dn_mult=CONFIG.ml_label_dn_atr,
    )
    return y_all[idx], w_all[idx]


def _fit_fold(lgb, X_tr, y_tr, w_tr, X_te, y_te, params):
    """Fit one fold, return (model, fold_auc)."""
    dtrain = lgb.Dataset(X_tr, label=y_tr, weight=w_tr)
    model = lgb.train(params, dtrain, num_boost_round=CONFIG.ml_num_rounds)
    p = model.predict(X_te)
    return model, _auc(y_te, p)


def _auc(y_true: np.ndarray, score: np.ndarray) -> float:
    """ROC-AUC without sklearn (rank statistic). Returns 0.5 if degenerate."""
    y = (np.asarray(y_true) > 0).astype(int)
    n_pos, n_neg = int(y.sum()), int((1 - y).sum())
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(score, kind="mergesort")
    ranks = np.empty(len(score), dtype=np.float64)
    ranks[order] = np.arange(1, len(score) + 1)
    return (ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def train(symbol: str, csv_path: str | None) -> None:
    try:
        import lightgbm as lgb
    except ImportError:
        raise SystemExit("lightgbm not installed. pip install lightgbm scikit-learn")

    bars = load_history(symbol, csv_path=csv_path)
    if len(bars.get("close", [])) < CONFIG.sma_slow + CONFIG.ml_label_horizon + 50:
        raise SystemExit(f"not enough bars for {symbol} "
                         f"({len(bars.get('close', []))}) — record more or widen the CSV")

    X, idx = build_features(bars["close"], bars.get("high") or bars["close"],
                            bars.get("low") or bars["close"])
    y, w = _labels_for(bars, idx, CONFIG.ml_label_horizon)

    # binary up/down target; drop the rare undefined (0) labels
    keep = y != 0
    X, y, w = X[keep], (y[keep] > 0).astype(int), w[keep]
    log.info(f"{symbol}: {len(X)} samples, {int(y.sum())} up / {int((1-y).sum())} down")

    params = {
        "objective": "binary", "metric": "auc",
        "learning_rate": CONFIG.ml_learning_rate, "num_leaves": CONFIG.ml_num_leaves,
        "min_data_in_leaf": CONFIG.ml_min_leaf, "feature_fraction": 0.8,
        "bagging_fraction": 0.8, "bagging_freq": 1, "verbosity": -1, "seed": 42,
    }
    cv = PurgedKFold(n_splits=CONFIG.ml_cv_splits,
                     label_horizon=CONFIG.ml_label_horizon, embargo_frac=0.01)
    aucs: list[float] = []
    for k, (tr, te) in enumerate(cv.split(len(X))):
        _, auc = _fit_fold(lgb, X[tr], y[tr], w[tr], X[te], y[te], params)
        aucs.append(auc)
        log.info(f"  fold {k}: AUC={auc:.4f}  (train={len(tr)} test={len(te)})")
    mean_auc = float(np.mean(aucs)) if aucs else 0.5
    log.info(f"{symbol}: purged CV AUC = {mean_auc:.4f} ± {np.std(aucs):.4f}")
    if mean_auc <= 0.52:
        log.warning("CV AUC ≤ 0.52 — NO reliable edge. Do not deploy; iterate on features.")

    # final fit on all data, persist model + feature contract
    final = lgb.train(params, lgb.Dataset(X, label=y, weight=w),
                      num_boost_round=CONFIG.ml_num_rounds)
    (ROOT / CONFIG.ml_model_path).parent.mkdir(parents=True, exist_ok=True)
    final.save_model(str(ROOT / CONFIG.ml_model_path))
    (ROOT / CONFIG.ml_features_path).write_text(json.dumps(list(FEATURE_NAMES)))
    log.info(f"saved → {CONFIG.ml_model_path} (+ feature contract). "
             f"Set ML_SIGNAL_ENABLED=true to use it live once CV AUC justifies it.")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Train the ML quant signal.")
    ap.add_argument("--symbol", required=True, help="symbol to train (e.g. ES)")
    ap.add_argument("--csv", default=None, help="OHLCV CSV bootstrap; omit to use recorded feed")
    args = ap.parse_args()
    train(args.symbol, args.csv)


if __name__ == "__main__":
    main()
