"""Add the MLL-protection guardrails (adapted to 1-trade-per-night overnight) to the
best config (1x MNQ, evening 18->06, $500 stop) and measure P(pass) lift vs the 48%
baseline. EXPLORATORY / conditional on the edge persisting (forward log is the test).

Guardrails (a-priori, not tuned):
  * LOSS-STREAK-HALT: after N consecutive losing NIGHTS, sit out the next night (reset).
  * PROFIT-LOCK: once account >= +$LOCK, stop trading (bank the run) IF >= target, else
    de-risk to protect the buffer (here: halt for the run once >= target reached).
Also prints the win/loss profile (answers 'avg win/loss, R:R').

Usage: .venv/bin/python oos/overnight_eval_pass_guarded.py
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


def pool(sym, stop):
    return np.array([r[1] for r in simulate(sym, stop) if r[0] >= 2022])


def make_draws(rng, base, size, block_len):
    """IID (block_len<=1) or block-bootstrap (preserves consecutive-night clustering)."""
    p = base * size
    if block_len <= 1:
        return rng.choice(p, size=(N_PATHS, MAX_NIGHTS), replace=True)
    nb = MAX_NIGHTS // block_len + 1
    starts = rng.integers(0, len(p) - block_len, size=(N_PATHS, nb))
    idx = starts[:, :, None] + np.arange(block_len)[None, None, :]
    return p[idx].reshape(N_PATHS, -1)[:, :MAX_NIGHTS]


def run_guarded(base, size, halt_n=0, block_len=1, seed=SEED):
    """Night-loop MC with optional loss-streak-halt. block_len>1 = block bootstrap.
    Returns P(pass), P(fail), median nights-to-pass."""
    rng = np.random.default_rng(seed)
    draws = make_draws(rng, base, size, block_len)
    bal = np.full(N_PATHS, START)
    floor = np.full(N_PATHS, START - TRAIL)
    streak = np.zeros(N_PATHS, int)
    alive = np.ones(N_PATHS, bool)
    outcome = np.zeros(N_PATHS, int)   # 0 pending, 1 pass, -1 fail
    day_pass = np.full(N_PATHS, MAX_NIGHTS + 1)
    for t in range(MAX_NIGHTS):
        trade = alive & ~((halt_n > 0) & (streak >= halt_n))
        pnl = np.where(trade, draws[:, t], 0.0)
        # streak resets on a halt night (we sat out) or a win; increments on a loss
        streak = np.where(~trade, 0, np.where(pnl < 0, streak + 1, 0))
        # DLL: a single traded night below -DLL fails
        dll = alive & trade & (pnl <= -DLL)
        newbal = bal + pnl
        # EOD trailing floor (locks at breakeven once +TRAIL banked)
        newfloor = np.where(alive, np.minimum(START, np.maximum(floor, newbal - TRAIL)), floor)
        mll = alive & (newbal <= newfloor)
        fail = dll | mll
        outcome = np.where(alive & fail, -1, outcome)
        bal, floor = newbal, newfloor
        won = alive & ~fail & (bal >= START + TARGET)
        outcome = np.where(won, 1, outcome)
        day_pass = np.where(won & (day_pass > MAX_NIGHTS), t, day_pass)
        alive = alive & ~fail & ~won
        if not alive.any():
            break
    tt = day_pass[outcome == 1]
    return (float((outcome == 1).mean()), float((outcome == -1).mean()),
            float(np.median(tt)) if tt.size else None)


def main():
    base = pool("MNQ", 500)
    wins = base[base > 0]
    loss = base[base <= 0]
    print("=" * 74)
    print("  1x MNQ evening 18->06 + $500 stop -- win/loss profile (2022-26)")
    print("=" * 74)
    print(f"  n={len(base)}  win%={100*len(wins)/len(base):.0f}  avg win=${wins.mean():+.0f}  "
          f"avg loss=${loss.mean():+.0f}  R:R={abs(wins.mean()/loss.mean()):.2f}  "
          f"mean=${base.mean():+.1f}/night  worst=${base.min():,.0f}")
    print("\n  Loss-streak-halt lift on P(pass) (1x MNQ):")
    print(f"  {'guard':<26}{'P(pass)':>10}{'P(fail)':>9}{'med nights':>12}")
    for bl, bname in ((1, "IID"), (20, "BLOCK-20 (clustering)")):
        print(f"  -- {bname} bootstrap --")
        for label, hn in (("  no guard", 0), ("  halt after 2 losses", 2),
                          ("  halt after 3 losses", 3)):
            pp, pf, md = run_guarded(base, 1, halt_n=hn, block_len=bl)
            print(f"  {label:<26}{pp:>10.3f}{pf:>9.3f}{(md if md else 0):>12.0f}")


if __name__ == "__main__":
    main()
