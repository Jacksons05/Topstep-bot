"""Eval-pass risk/consistency architecture (edge-independent variance control).

Covers the loss-streak de-risk, loss-streak day halt, profit-lock, and
MLL-headroom sizing taper added to TopstepRiskManager. These NEVER loosen a
Topstep hard rule — they only reduce size or block new entries.
"""
from __future__ import annotations

import dataclasses

import pytest

import topstep_risk as tr
from config import CONFIG


def _mgr(**cfg):
    """Fresh manager at a fixed start equity, with optional CONFIG overrides
    applied to the module CONFIG the manager reads."""
    if cfg:
        tr.CONFIG = dataclasses.replace(CONFIG, **cfg)
    else:
        tr.CONFIG = CONFIG
    m = tr.TopstepRiskManager(initial_equity=50_000.0)
    m.reset_day(50_000.0)
    return m


def teardown_function():
    tr.CONFIG = CONFIG  # restore module CONFIG after each test


def _loss(m, n=1):
    for _ in range(n):
        m.record_close(-100.0, 60.0)


def _win(m, amt=100.0):
    m.record_close(amt, 60.0)


# ── loss-streak de-risk sizing ───────────────────────────────────────────────

def test_size_unhaircut_before_threshold():
    m = _mgr(topstep_loss_derisk_after=2, topstep_headroom_full_usd=0.0)
    _loss(m, 1)
    assert m.combined_size_mult(50_000.0) == 1.0


def test_size_geometric_derisk_after_losses():
    m = _mgr(topstep_loss_derisk_after=2, topstep_loss_derisk_mult=0.5,
             topstep_loss_derisk_floor=0.25, topstep_headroom_full_usd=0.0)
    _loss(m, 2)
    assert m.combined_size_mult(50_000.0) == 0.5     # 2 losses → 0.5
    _loss(m, 1)
    assert m.combined_size_mult(50_000.0) == 0.25    # 3 → 0.25 (floor)
    _loss(m, 1)
    assert m.combined_size_mult(50_000.0) == 0.25    # 4 → still floored


def test_win_resets_loss_streak():
    m = _mgr(topstep_loss_derisk_after=2, topstep_headroom_full_usd=0.0)
    _loss(m, 3)
    assert m.combined_size_mult(50_000.0) < 1.0
    _win(m)
    assert m.combined_size_mult(50_000.0) == 1.0


def test_scratch_breaks_loss_streak_without_win():
    m = _mgr(topstep_loss_derisk_after=2, topstep_headroom_full_usd=0.0)
    _loss(m, 2)
    m.record_close(0.0, 60.0)                          # scratch
    assert m._consec_losses == 0
    assert m.combined_size_mult(50_000.0) == 1.0


# ── loss-streak day halt ─────────────────────────────────────────────────────

def test_loss_streak_halt_blocks_after_n():
    m = _mgr(topstep_loss_streak_halt=3)
    _loss(m, 2)
    assert m.loss_streak_ok()[0] is True
    _loss(m, 1)
    ok, why = m.loss_streak_ok()
    assert ok is False and "loss-streak" in why


def test_loss_streak_halt_off_when_zero():
    m = _mgr(topstep_loss_streak_halt=0)
    _loss(m, 10)
    assert m.loss_streak_ok()[0] is True


# ── profit-lock ──────────────────────────────────────────────────────────────

def test_profit_lock_arms_then_halts_on_giveback():
    m = _mgr(topstep_profit_lock_usd=750.0, topstep_profit_lock_giveback=0.5)
    _win(m, 800.0)                # day peak +800, above trigger
    assert m.profit_lock_ok()[0] is True          # no giveback yet
    m.record_close(-500.0, 60.0)  # gave back 500 of 800 (>50%)
    ok, why = m.profit_lock_ok()
    assert ok is False and "profit-lock" in why


def test_profit_lock_dormant_below_trigger():
    m = _mgr(topstep_profit_lock_usd=750.0)
    _win(m, 300.0)
    m.record_close(-250.0, 60.0)   # gave back most of it, but never hit trigger
    assert m.profit_lock_ok()[0] is True


# ── MLL-headroom sizing taper ────────────────────────────────────────────────

def test_headroom_full_size_when_far_from_floor():
    m = _mgr(topstep_headroom_full_usd=2_000.0, topstep_headroom_size_floor=0.25,
             topstep_loss_derisk_after=99)
    # floor is 48_000 (peak 50k - 2k buffer). equity 50_000 → headroom 2_000 = full.
    assert m.combined_size_mult(50_000.0) == 1.0


def test_headroom_tapers_toward_floor():
    m = _mgr(topstep_headroom_full_usd=2_000.0, topstep_headroom_size_floor=0.25,
             topstep_loss_derisk_after=99)
    # headroom 1_000 (equity 49_000, floor 48_000) → 0.25 + 0.75*0.5 = 0.625
    assert m.combined_size_mult(49_000.0) == pytest.approx(0.625)
    # at the floor → floor multiplier
    assert m.combined_size_mult(48_000.0) == pytest.approx(0.25)


def test_multipliers_compound_and_cap_at_one():
    m = _mgr(topstep_loss_derisk_after=2, topstep_loss_derisk_mult=0.5,
             topstep_headroom_full_usd=2_000.0, topstep_headroom_size_floor=0.25)
    _loss(m, 2)                       # loss mult 0.5
    # equity 49_000 → headroom mult 0.625; product 0.3125
    assert m.combined_size_mult(49_000.0) == pytest.approx(0.5 * 0.625)
    # never exceeds 1.0 even with no penalties
    m2 = _mgr(topstep_loss_derisk_after=99, topstep_headroom_full_usd=0.0)
    assert m2.combined_size_mult(60_000.0) == 1.0


def test_sizing_only_ever_reduces_never_amplifies():
    """Core invariant: on a funded account these levers can only DE-RISK."""
    m = _mgr()
    for eq in (48_000.0, 49_000.0, 50_000.0, 55_000.0, 100_000.0):
        assert 0.0 < m.combined_size_mult(eq) <= 1.0
