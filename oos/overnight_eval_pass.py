"""Does the STOPPED evening overnight drift pass a $50k TopstepX Combine, and how
fast? Bootstraps the EMPIRICAL per-night P&L (from overnight_stop_topstep.simulate)
against the confirmed rules. CONDITIONAL: assumes the historical edge persists forward
(holdout spent; forward log is the real test) -- this is a RISK/timing sim, not a
validation of the edge.

Rules: start $50k; per-night DLL $1,000 (a single night's loss can't exceed it);
trailing MLL $2,000 (ratchets on EOD balance, locks at $50k once EOD >= $52k); pass at
+$3,000 with consistency (best night < 50% of profit -> auto-satisfied by the stop).
One trade per night. Seed-controlled.

Usage: .venv/bin/python oos/overnight_eval_pass.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from overnight_stop_topstep import simulate  # noqa: E402

START, TARGET, TRAIL, DLL = 50_000.0, 3_000.0, 2_000.0, 1_000.0
SEED, MAX_NIGHTS, N_PATHS = 20260721, 400, 20_000


def pnl_series(sym, stop, use_recent=True):
    rows = simulate(sym, stop)
    if use_recent:
        rows = [r for r in rows if r[0] >= 2022]   # post-decay regime only (conservative)
    return np.array([r[1] for r in rows])


def eval_pass(base_pnl, size, n_paths=N_PATHS, seed=SEED):
    rng = np.random.default_rng(seed)
    pool = base_pnl * size
    draws = rng.choice(pool, size=(n_paths, MAX_NIGHTS), replace=True)
    bal = START + np.cumsum(draws, axis=1)
    floor = np.maximum.accumulate(
        np.concatenate([np.full((n_paths, 1), START - TRAIL),
                        np.minimum(START, bal - TRAIL)], axis=1), axis=1)[:, 1:]
    # lock at 50k once bal hits 52k: floor never exceeds START
    floor = np.minimum(floor, START)
    mll_breach = bal <= floor
    dll_breach = draws <= -DLL                 # a single night exceeding the DLL
    hit_target = bal >= START + TARGET
    big = MAX_NIGHTS + 1
    first_mll = np.where(mll_breach.any(1), mll_breach.argmax(1), big)
    first_dll = np.where(dll_breach.any(1), dll_breach.argmax(1), big)
    first_tgt = np.where(hit_target.any(1), hit_target.argmax(1), big)
    first_fail = np.minimum(first_mll, first_dll)
    passed = first_tgt < first_fail
    failed = first_fail < first_tgt
    tt = first_tgt[passed]
    return {
        "p_pass": float(passed.mean()),
        "p_fail": float(failed.mean()),
        "median_nights_to_pass": float(np.median(tt)) if tt.size else None,
        "mean_pnl_per_night": float(base_pnl.mean() * size),
    }


def main():
    print("=" * 76)
    print("  Stopped EVENING overnight drift vs $50k Combine (2022-26 P&L, $500 stop)")
    print("  CONDITIONAL on the edge persisting forward (forward log is the real test)")
    print("=" * 76)
    print(f"  {'config':<22}{'$/night':>9}{'P(pass)':>10}{'P(fail)':>9}{'med nights':>12}")
    configs = [
        ("MES", 500, 1), ("MES", 500, 2), ("MES", 500, 3),
        ("MNQ", 500, 1), ("MNQ", 500, 2),
    ]
    for sym, stop, size in configs:
        base = pnl_series(sym, stop)
        r = eval_pass(base, size)
        md = r["median_nights_to_pass"]
        print(f"  {f'{size}x {sym} stop${stop}':<22}{r['mean_pnl_per_night']:>+9.1f}"
              f"{r['p_pass']:>10.3f}{r['p_fail']:>9.3f}"
              f"{(md if md else 0):>12.0f}")
    print("\n  median nights-to-pass ~= trading days (nights). Passing is SLOW because the")
    print("  per-contract edge is small and the crisis tail caps safe size. This is a")
    print("  survivable, positive, slow grinder -- IF the edge persists (forward log).")


if __name__ == "__main__":
    main()
