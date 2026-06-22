"""ML quant-signal stack: features, labeling, purged CV, graceful fallback.

These run WITHOUT lightgbm installed — they exercise the pure logic and the
fallback contract (no model → engine keeps using signals.quant_signal).
"""
from __future__ import annotations

import numpy as np
import pytest

from features import FEATURE_NAMES, build_features, feature_row, row_to_vector
from mlcv import PurgedKFold, triple_barrier_labels
from ml_signal import MLQuant


def _ramp(n: int = 120) -> dict:
    """Deterministic rising series with noise → enough bars to compute features."""
    rng = np.random.default_rng(0)
    close = np.cumsum(rng.normal(0.1, 1.0, n)) + 100.0
    high = close + np.abs(rng.normal(0.5, 0.2, n))
    low = close - np.abs(rng.normal(0.5, 0.2, n))
    return {"close": close.tolist(), "high": high.tolist(), "low": low.tolist()}


# ── features ───────────────────────────────────────────────────────────────
def test_feature_row_full_contract():
    bars = _ramp()
    row = feature_row(bars)
    assert row is not None
    # every declared feature is present and ordered vector matches the contract
    assert set(row) == set(FEATURE_NAMES)
    vec = row_to_vector(row)
    assert vec.shape == (len(FEATURE_NAMES),)


def test_feature_row_too_few_bars_returns_none():
    assert feature_row({"close": [100.0, 101.0]}) is None


def test_micro_features_nan_without_orderflow():
    row = feature_row(_ramp())
    # micro slice is NaN when no live order-flow snapshot is supplied
    assert np.isnan(row["obi"]) and np.isnan(row["cvd"])


def test_micro_features_populated_with_orderflow():
    micro = {"obi": 0.4, "cvd": 1200.0, "micro_price": 100.2,
             "bid": 100.0, "ask": 100.4, "whale": 1, "cvd_div": "bullish"}
    row = feature_row(_ramp(), micro)
    assert row["obi"] == pytest.approx(0.4)
    assert row["whale"] == 1.0
    assert row["cvd_div"] == 1.0  # bullish → +1


def test_build_features_is_causal_and_aligned():
    bars = _ramp(200)
    X, idx = build_features(bars["close"], bars["high"], bars["low"])
    assert X.shape[0] == len(idx)
    assert X.shape[1] == len(FEATURE_NAMES)
    assert idx == sorted(idx) and idx[-1] == len(bars["close"]) - 1


# ── triple-barrier labeling ─────────────────────────────────────────────────
def test_triple_barrier_up_barrier_first():
    # straight ramp up → up barrier should be hit first → label +1
    n = 30
    close = np.linspace(100.0, 110.0, n)
    high = close + 0.1
    low = close - 0.1
    atr = np.full(n, 1.0)
    labels, weights = triple_barrier_labels(close, high, low, atr,
                                            horizon=10, up_mult=1.0, dn_mult=1.0)
    assert labels[0] == 1
    assert weights[0] > 0


def test_triple_barrier_down_barrier_first():
    n = 30
    close = np.linspace(110.0, 100.0, n)
    high = close + 0.1
    low = close - 0.1
    atr = np.full(n, 1.0)
    labels, _ = triple_barrier_labels(close, high, low, atr,
                                      horizon=10, up_mult=1.0, dn_mult=1.0)
    assert labels[0] == -1


# ── purged CV ────────────────────────────────────────────────────────────────
def test_purged_kfold_no_train_test_overlap_and_purge():
    h, embargo_frac, n = 10, 0.02, 500
    cv = PurgedKFold(n_splits=5, label_horizon=h, embargo_frac=embargo_frac)
    embargo = int(n * embargo_frac)
    for train_idx, test_idx in cv.split(n):
        # disjoint train/test
        assert not (set(train_idx) & set(test_idx))
        lo, hi = test_idx.min(), test_idx.max()
        # no train index falls in the purged+embargoed gap around the test block:
        # purge label-overlap before ([lo-h, lo)), embargo after ((hi, hi+h+embargo])
        gap = (train_idx >= lo - h) & (train_idx <= hi + h + embargo)
        assert not gap.any()


def test_purged_kfold_rejects_bad_args():
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=1)


# ── fallback contract ────────────────────────────────────────────────────────
def test_mlquant_not_ready_without_model(tmp_path):
    m = MLQuant(model_path=str(tmp_path / "nope.txt"))
    assert m.ready is False
    # read() returns None so the engine falls back to quant_signal
    assert m.read(_ramp()) is None
    assert m.win_prob(_ramp()) is None
