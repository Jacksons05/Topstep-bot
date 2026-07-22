"""Quant survey of canonical intraday day-trading edge FAMILIES not yet covered, each
under two filters: MULTI-ERA robustness + TopstepX fit (tail vs $1k DLL, flat by close).
EXPLORATORY (holdout spent -> hypothesis generation, not validation).

Families here:
  1. VOLATILITY BREAKOUT (Crabel/NR): after a compressed prior-day range (bottom tercile
     of trailing 20), trade the FIRST break of today's opening 30-min range, hold to close.
     Thesis: compression precedes expansion; the breakout starts a trend day.
  2. MOMENTUM LOOKBACK SWEEP: enter at 09:30+L in the direction of the 09:30->(09:30+L)
     move, hold to close, for L in {30,60,120} min. (Time-series momentum, intraday.)
  3. RANGE-DAY FADE: after a WIDE prior-day range (top tercile), fade the opening 30-min
     move (mean-revert on high-range/choppy regimes).

Usage: .venv/bin/python oos/intraday_edge_survey.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

OPEN, CLOSE = 9 * 60 + 30, 16 * 60


def era(y):
    return "2010-15" if y <= 2015 else "2016-19" if y <= 2019 else "2020-21" if y <= 2021 else "2022-26"


def _px(ts, arr, idxs, m):
    for j in idxs:
        if C.mins(ts[j]) >= m:
            return arr[j]
    return None


def summ(name, pnl, yr):
    pnl = np.asarray(pnl, float)
    if len(pnl) < 30:
        print(f"  {name:<22} n={len(pnl)} (too few)")
        return
    be = []
    for e in ("2010-15", "2016-19", "2020-21", "2022-26"):
        mm = np.array([era(y) == e for y in yr])
        be.append(f"{pnl[mm].mean():+.0f}" if mm.any() else "--")
    npos = sum(1 for x in be if x != "--" and float(x) > 0)
    t = pnl.mean() / (pnl.std() / np.sqrt(len(pnl))) if pnl.std() > 0 else 0
    flag = "  <== MULTI-ERA+" if npos >= 3 and pnl.mean() > 0 else ""
    print(f"  {name:<22}{pnl.mean():>+8.1f}{pnl.mean()/pnl.std():>8.3f}{t:>7.2f}"
          f"{100*(pnl>0).mean():>6.0f}%{pnl.min():>9,.0f}   {'/'.join(be)}{flag}")


def main():
    for sym in ("ES", "MNQ"):
        ts, o, h, l, c, v = C.load(sym)
        by_date = defaultdict(list)
        for i, t in enumerate(ts):
            if t.weekday() < 5 and OPEN <= C.mins(t) <= CLOSE:
                by_date[t.date()].append(i)
        days = sorted(by_date)
        spec = C.SPECS[sym]
        cost = spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
        pv = spec["pt"]
        print("=" * 88)
        print(f"  {sym}  intraday edge survey (LONG/SHORT $/1ct net 1t)  EXPLORATORY")
        print(f"  {'family':<22}{'mean$':>8}{'Sharpe':>8}{'t':>7}{'win%':>6}{'worst$':>9}"
              f"   era 10-15/16-19/20-21/22-26")

        prange = [None] * len(days)
        for k in range(1, len(days)):
            pidx = by_date[days[k - 1]]
            prange[k] = max(h[j] for j in pidx) - min(l[j] for j in pidx)

        # collect per-day features
        volbrk, momo = {30: [], 60: [], 120: []}, {30: [], 60: [], 120: []}
        volbrk_y, momo_y = {30: [], 60: [], 120: []}, {30: [], 60: [], 120: []}
        vb_pnl, vb_y, rf_pnl, rf_y = [], [], [], []
        for k in range(1, len(days)):
            idx = by_date[days[k]]
            op = _px(ts, o, idx, OPEN)
            cl = c[idx[-1]]
            if op is None:
                continue
            yr = days[k].year
            # momentum lookback sweep
            for L in (30, 60, 120):
                pL = _px(ts, o, idx, OPEN + L)
                if pL is not None:
                    momo[L].append(np.sign(pL - op) * (cl - pL) * pv - cost)
                    momo_y[L].append(yr)
            # compression / expansion regime (prior range terciles vs trailing 20)
            if k >= 21 and prange[k] is not None:
                hist = [prange[j] for j in range(k - 20, k) if prange[j] is not None]
                if len(hist) >= 10:
                    lo, hi = np.percentile(hist, 33.33), np.percentile(hist, 66.67)
                    # today's opening 30-min range
                    or_idx = [j for j in idx if C.mins(ts[j]) < OPEN + 30]
                    if or_idx:
                        orh, orl = max(h[j] for j in or_idx), min(l[j] for j in or_idx)
                        # first break of the OR after 10:00
                        side, epx = 0, None
                        for j in [x for x in idx if C.mins(ts[x]) >= OPEN + 30][:-1]:
                            nxt = idx[idx.index(j) + 1]
                            if h[j] > orh:
                                side, epx = 1, o[nxt]; break
                            if l[j] < orl:
                                side, epx = -1, o[nxt]; break
                        if side != 0:
                            pnl = side * (cl - epx) * pv - cost
                            if prange[k] <= lo:            # compressed prior day
                                vb_pnl.append(pnl); vb_y.append(yr)
                            if prange[k] >= hi:            # wide prior day -> fade the OR move
                                p10 = _px(ts, o, idx, OPEN + 30)
                                if p10 is not None:
                                    rf_pnl.append(-np.sign(p10 - op) * (cl - p10) * pv - cost)
                                    rf_y.append(yr)
        for L in (30, 60, 120):
            summ(f"momentum {L}min->close", momo[L], momo_y[L])
        summ("vol-breakout (compress)", vb_pnl, vb_y)
        summ("range-day fade (wide)", rf_pnl, rf_y)
        print()


if __name__ == "__main__":
    main()
