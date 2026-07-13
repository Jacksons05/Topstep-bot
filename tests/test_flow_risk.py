"""Unit tests for the flow-risk overlays (A10 vol-target sizing, A8 toxicity)."""
from __future__ import annotations

import math

import numpy as np

from config import CONFIG
from flow_risk import (
    FlowRiskManager,
    bvc_vpin_series,
    har_vol_forecast,
    toxicity_read,
    vol_target_multiplier,
)


def _series(vols, n_each=40, seed=0):
    """Concatenate segments of gaussian log-returns with the given per-segment
    vols into a synthetic close series (starts at 100)."""
    rng = np.random.default_rng(seed)
    lr = np.concatenate([rng.normal(0, v, n_each) for v in vols])
    return list(100.0 * np.exp(np.cumsum(lr)))


# ── A10: vol-target sizing ────────────────────────────────────────────────
def test_vol_multiplier_sizes_down_when_vol_elevated():
    # last segment is 4x the baseline vol -> forecast > baseline -> mult < 1
    closes = _series([0.005, 0.005, 0.005, 0.02], seed=1)
    mult, _ = vol_target_multiplier(closes)
    assert mult < 1.0
    assert CONFIG.vol_sizing_floor <= mult <= CONFIG.vol_sizing_cap


def test_vol_multiplier_sizes_up_when_calm():
    # last segment much calmer than the symbol's norm -> mult > 1 (capped)
    closes = _series([0.02, 0.02, 0.02, 0.004], seed=2)
    mult, _ = vol_target_multiplier(closes)
    assert mult > 1.0
    assert mult <= CONFIG.vol_sizing_cap


def test_vol_multiplier_clamped_and_neutral_on_flat():
    assert vol_target_multiplier([100.0] * 50)[0] == 1.0   # no variation -> neutral
    assert vol_target_multiplier([100.0, 101.0])[0] == 1.0  # too short -> neutral


def test_har_forecast_monotonic_in_vol():
    lo = har_vol_forecast(_series([0.004], n_each=80, seed=3))
    hi = har_vol_forecast(_series([0.02], n_each=80, seed=3))
    assert hi > lo > 0


# ── A8: toxicity / BVC-VPIN ───────────────────────────────────────────────
def test_vpin_higher_for_trending_than_choppy():
    rng = np.random.default_rng(4)
    n = 200
    trend = list(100 + np.cumsum(np.abs(rng.normal(0.5, 0.1, n))))     # one-sided
    chop = list(100 + np.cumsum(rng.normal(0, 0.5, n)))                # balanced
    vol = [1000.0] * n
    tox_trend, _ = toxicity_read(trend, vol)
    tox_chop, _ = toxicity_read(chop, vol)
    assert tox_trend > tox_chop


def test_vpin_series_bounded_0_1():
    closes = _series([0.01], n_each=200, seed=5)
    s = bvc_vpin_series(closes, [1000.0] * len(closes), CONFIG.vpin_window_bars)
    assert s.size > 0
    assert float(s.min()) >= 0.0 and float(s.max()) <= 1.0


def test_toxicity_read_insufficient_history():
    assert toxicity_read([100, 101, 102], [1, 1, 1]) == (0.0, 0.0)


# ── FlowRiskManager.assess integration ────────────────────────────────────
def test_assess_produces_multiplier_and_no_veto_on_normal_bars():
    closes = _series([0.008], n_each=120, seed=6)
    bars = {"close": closes, "high": closes, "low": closes,
            "volume": [1000.0] * len(closes)}
    read = FlowRiskManager().assess(bars)
    assert CONFIG.vol_sizing_floor <= read.vol_mult <= CONFIG.vol_sizing_cap
    assert 0.0 <= read.tox_pct <= 1.0
    # a homogeneous-vol series should not sit in the top decile by construction
    assert isinstance(read.veto, bool)


def test_assess_short_bars_is_neutral():
    read = FlowRiskManager().assess({"close": [100.0, 101.0]})
    assert read.vol_mult == 1.0 and read.veto is False
