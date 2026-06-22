"""Labeling + leakage-safe cross-validation for the ML quant signal.

Two pieces, both deliberately dependency-light (numpy only — no mlfinlab):

  1. triple_barrier_labels — López de Prado's triple-barrier method. For each
     bar, set an ATR-scaled profit barrier and stop barrier and a vertical
     (time) barrier `horizon` bars out. Label = +1 if the up barrier is hit
     first, -1 if the down barrier is hit first, sign(return) on a timeout.
     This labels what a trade would ACTUALLY have done under an ATR bracket —
     the same exit logic risk.should_exit uses — instead of a naive fixed-
     horizon return.

  2. PurgedKFold — k-fold CV that PURGES training samples whose label window
     overlaps the test fold, then EMBARGOes a few samples after each test fold.
     Without this, a model trained on overlapping triple-barrier labels leaks
     future information into the test fold and the backtest lies. This is the
     single most important guard against the "great backtest, dead live" trap.
"""
from __future__ import annotations

from collections.abc import Iterator

import numpy as np


def triple_barrier_labels(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    atr: np.ndarray,
    *,
    horizon: int,
    up_mult: float,
    dn_mult: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Per-bar triple-barrier label + sample weight.

    Returns (labels, weights), each length len(closes):
      labels[i]  ∈ {-1, 0, +1}  (+1 up-barrier first, -1 down-barrier first,
                  sign of horizon return on timeout, 0 only when undefined —
                  no ATR or no forward bars).
      weights[i] = |realized return to the touch|, used to weight the fit toward
                  decisive moves. 0 where the label is undefined.

    Causal: bar i's label only looks at bars i+1..i+horizon, so a feature row
    computed at bar i (features.build_features) pairs with a forward-only label.
    """
    closes = np.asarray(closes, dtype=np.float64)
    highs = np.asarray(highs, dtype=np.float64)
    lows = np.asarray(lows, dtype=np.float64)
    atr = np.asarray(atr, dtype=np.float64)
    n = len(closes)
    labels = np.zeros(n, dtype=np.int8)
    weights = np.zeros(n, dtype=np.float64)

    for i in range(n - 1):
        entry = closes[i]
        a = atr[i]
        if entry <= 0 or not np.isfinite(a) or a <= 0:
            continue
        up = entry + up_mult * a
        dn = entry - dn_mult * a
        end = min(i + horizon, n - 1)
        hit = 0
        touch_px = closes[end]
        for j in range(i + 1, end + 1):
            if highs[j] >= up:
                hit, touch_px = 1, up
                break
            if lows[j] <= dn:
                hit, touch_px = -1, dn
                break
        if hit == 0:  # vertical-barrier timeout → sign of the realized move
            ret = (closes[end] - entry) / entry
            hit = 1 if ret > 0 else (-1 if ret < 0 else 0)
            touch_px = closes[end]
        labels[i] = hit
        weights[i] = abs(touch_px - entry) / entry
    return labels, weights


class PurgedKFold:
    """K-fold CV with purge + embargo for overlapping-label time series.

    Each sample i has a label that spans [i, i + label_horizon]. A training
    sample whose label window overlaps the test fold's index range is PURGED
    (dropped). After the test fold, an EMBARGO of `embargo_frac * n` samples is
    also dropped from training to kill serial-correlation leakage at the seam.

    Folds are contiguous in time (no shuffling) — order matters for markets.
    """

    def __init__(self, n_splits: int = 5, label_horizon: int = 20, embargo_frac: float = 0.01):
        if n_splits < 2:
            raise ValueError("n_splits must be >= 2")
        self.n_splits = n_splits
        self.label_horizon = max(0, int(label_horizon))
        self.embargo_frac = max(0.0, float(embargo_frac))

    def split(self, n: int) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """Yield (train_idx, test_idx) arrays for a dataset of length n."""
        if n < self.n_splits:
            raise ValueError(f"n={n} < n_splits={self.n_splits}")
        indices = np.arange(n)
        embargo = int(n * self.embargo_frac)
        fold_bounds = np.array_split(indices, self.n_splits)
        for fold in fold_bounds:
            test_start, test_end = fold[0], fold[-1]
            test_idx = indices[test_start : test_end + 1]
            # purge any train sample whose label window [i, i+h] reaches the
            # test block, and embargo the band right after the test block.
            purge_lo = test_start - self.label_horizon
            embargo_hi = test_end + self.label_horizon + embargo
            train_mask = (indices < purge_lo) | (indices > embargo_hi)
            train_idx = indices[train_mask]
            if len(train_idx) and len(test_idx):
                yield train_idx, test_idx
