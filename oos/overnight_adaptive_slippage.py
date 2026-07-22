"""Two final legitimate tests for the overnight config (EXPLORATORY; conditional on
edge persisting; holdout spent):
  1. EXECUTION REALISM: how does the edge + P(pass) degrade at 1 / 2 / 3-tick slippage
     at the 18:00 ET reopen? (Is +$10/night real net of an honest fill?)
  2. ADAPTIVE SIZING: 1x MNQ ($500 stop) until the trailing floor LOCKS at breakeven
     (bal >= $52k), then 2x MNQ ($400 stop, so 2*400=$800 < $1k DLL) for the final push.
     Does ramping on the safe leg beat static 1x?

Block-bootstrap (block=20) preserves clustering; loss-streak-halt after 2 losing nights.

Usage: .venv/bin/python oos/overnight_adaptive_slippage.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from overnight_stop_topstep import simulate  # noqa: E402

START, TARGET, TRAIL, DLL = 50_000.0, 3_000.0, 2_000.0, 1_000.0
SEED, MAX_NIGHTS, N_PATHS, BLOCK = 20260721, 400, 20_000, 20


def block_idx(rng, npool):
    nb = MAX_NIGHTS // BLOCK + 1
    starts = rng.integers(0, npool - BLOCK, size=(N_PATHS, nb))
    return (starts[:, :, None] + np.arange(BLOCK)[None, None, :]).reshape(N_PATHS, -1)[:, :MAX_NIGHTS]


def eval_paths(pnl_by_night, halt=2):
    """pnl_by_night: (N_PATHS, MAX_NIGHTS) already sized. Returns P(pass),P(fail),median."""
    bal = np.full(N_PATHS, START)
    floor = np.full(N_PATHS, START - TRAIL)
    streak = np.zeros(N_PATHS, int)
    alive = np.ones(N_PATHS, bool)
    outcome = np.zeros(N_PATHS, int)
    dpass = np.full(N_PATHS, MAX_NIGHTS + 1)
    for t in range(MAX_NIGHTS):
        trade = alive & ~(streak >= halt)
        pnl = np.where(trade, pnl_by_night[:, t], 0.0)
        streak = np.where(~trade, 0, np.where(pnl < 0, streak + 1, 0))
        dll = alive & trade & (pnl <= -DLL)
        bal = bal + pnl
        floor = np.where(alive, np.minimum(START, np.maximum(floor, bal - TRAIL)), floor)
        fail = dll | (alive & (bal <= floor))
        outcome = np.where(alive & fail, -1, outcome)
        won = alive & ~fail & (bal >= START + TARGET)
        outcome = np.where(won, 1, outcome)
        dpass = np.where(won & (dpass > MAX_NIGHTS), t, dpass)
        alive = alive & ~fail & ~won
        if not alive.any():
            break
    tt = dpass[outcome == 1]
    return float((outcome == 1).mean()), float((outcome == -1).mean()), \
        (float(np.median(tt)) if tt.size else None)


def adaptive_eval(p500, p400, seed=SEED):
    """1x p500 until floor locks (bal>=52k) then 2x p400. Block-bootstrapped indices."""
    rng = np.random.default_rng(seed)
    idx = block_idx(rng, len(p500))
    a500, a400 = p500[idx], p400[idx]
    bal = np.full(N_PATHS, START)
    floor = np.full(N_PATHS, START - TRAIL)
    locked = np.zeros(N_PATHS, bool)
    streak = np.zeros(N_PATHS, int)
    alive = np.ones(N_PATHS, bool)
    outcome = np.zeros(N_PATHS, int)
    dpass = np.full(N_PATHS, MAX_NIGHTS + 1)
    for t in range(MAX_NIGHTS):
        trade = alive & ~(streak >= 2)
        pnl = np.where(locked, a400[:, t] * 2, a500[:, t])
        pnl = np.where(trade, pnl, 0.0)
        streak = np.where(~trade, 0, np.where(pnl < 0, streak + 1, 0))
        dll = alive & trade & (pnl <= -DLL)
        bal = bal + pnl
        floor = np.where(alive, np.minimum(START, np.maximum(floor, bal - TRAIL)), floor)
        locked = locked | (bal >= START + TRAIL)
        fail = dll | (alive & (bal <= floor))
        outcome = np.where(alive & fail, -1, outcome)
        won = alive & ~fail & (bal >= START + TARGET)
        outcome = np.where(won, 1, outcome)
        dpass = np.where(won & (dpass > MAX_NIGHTS), t, dpass)
        alive = alive & ~fail & ~won
        if not alive.any():
            break
    tt = dpass[outcome == 1]
    return float((outcome == 1).mean()), float((outcome == -1).mean()), \
        (float(np.median(tt)) if tt.size else None)


def pool(sym, stop, slip=1):
    return np.array([r[1] for r in simulate(sym, stop, slip) if r[0] >= 2022])


def main():
    print("=" * 74)
    print("  1. EXECUTION REALISM -- 1x MNQ evening $500 stop at 1/2/3-tick slippage")
    print("=" * 74)
    print(f"  {'slippage':<12}{'$/night':>9}{'P(pass)':>10}{'P(fail)':>9}{'med nights':>12}")
    for slip in (1, 2, 3):
        p = pool("MNQ", 500, slip)
        rng = np.random.default_rng(SEED)
        sized = (p * 1)[block_idx(rng, len(p))]
        pp, pf, md = eval_paths(sized)
        print(f"  {f'{slip}-tick':<12}{p.mean():>+9.1f}{pp:>10.3f}{pf:>9.3f}{(md if md else 0):>12.0f}")

    print("\n" + "=" * 74)
    print("  2. ADAPTIVE SIZING -- static 1x vs (1x->2x after floor locks at breakeven)")
    print("=" * 74)
    p500 = pool("MNQ", 500)
    p400 = pool("MNQ", 400)
    rng = np.random.default_rng(SEED)
    static = eval_paths((p500 * 1)[block_idx(rng, len(p500))])
    adap = adaptive_eval(p500, p400)
    print(f"  {'config':<28}{'P(pass)':>10}{'P(fail)':>9}{'med nights':>12}")
    print(f"  {'static 1x MNQ $500':<28}{static[0]:>10.3f}{static[1]:>9.3f}{(static[2] or 0):>12.0f}")
    print(f"  {'adaptive 1x->2x (lock)':<28}{adap[0]:>10.3f}{adap[1]:>9.3f}{(adap[2] or 0):>12.0f}")
    print(f"\n  ($400-stop 2x leg: 2*$400=$800 < $1k DLL; only rare gap-through nights breach.)")


if __name__ == "__main__":
    main()
