"""MAX-EFFORT EXPLORATORY (holdout spent; hypothesis generation, NOT validation).
Hunt for a MULTI-ERA-robust intraday-off-open signal (the bar the overnight edge cleared
and drive-momentum-BIG failed = recent-only). Two untested, mechanistically-motivated
constructions:

  A. GAP x DRIVE agreement ("trend day"): gap = open-priorclose, drive = 10:00-open.
     - AGREE  (same sign): trade side=sign(drive) 10:00->close  (gap-and-go trend day)
     - DISAGREE (opp sign): trade side=sign(drive) 10:00->close  (the drive 'won' the open)
     Also the BIG-drive versions of each.
  B. PRIOR-DAY-RANGE breakout: first RTH break of prior-day high(->long)/low(->short),
     entered next bar, held to close.

Reports mean$/Sharpe/win/worst + by-era. Multi-era-positive = credible; recent-only = not.

Usage: .venv/bin/python oos/intraday_combo_explore.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

OPEN, T1000, CLOSE = 9 * 60 + 30, 10 * 60, 16 * 60


def era(y):
    return "2010-15" if y <= 2015 else "2016-19" if y <= 2019 else "2020-21" if y <= 2021 else "2022-26"


def _px(ts, arr, idxs, m):
    for j in idxs:
        if C.mins(ts[j]) >= m:
            return arr[j]
    return None


def summ(name, pnl, yr):
    if len(pnl) < 30:
        print(f"  {name:<20} n={len(pnl)} (too few)")
        return
    be = []
    for e in ("2010-15", "2016-19", "2020-21", "2022-26"):
        mm = np.array([era(y) == e for y in yr])
        be.append(f"{pnl[mm].mean():+.0f}" if mm.any() else "--")
    npos = sum(1 for x in be if x != "--" and float(x) > 0)
    flag = " <== multi-era+" if npos >= 3 else ""
    print(f"  {name:<20}{pnl.mean():>+8.1f}{pnl.mean()/pnl.std():>8.3f}{100*(pnl>0).mean():>6.0f}%"
          f"{pnl.min():>9,.0f}   {'/'.join(be)}{flag}")


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
        Y, GAP, DRV, RA = [], [], [], []      # year, gap, drive, 10:00->close move
        BRK = []                               # prior-day-range breakout pnl (net)
        for k, d in enumerate(days):
            if k == 0:
                continue
            pidx, idx = by_date[days[k - 1]], by_date[d]
            pc = c[pidx[-1]]
            pdh = max(h[j] for j in pidx)
            pdl = min(l[j] for j in pidx)
            op = _px(ts, o, idx, OPEN)
            p10 = _px(ts, o, idx, T1000)
            cl = c[idx[-1]]
            if None in (op, p10):
                continue
            Y.append(d.year); GAP.append(op - pc); DRV.append(p10 - op); RA.append(cl - p10)
            # prior-day-range breakout: first bar (after entry-warmup) that breaks pdh/pdl
            side, epx = 0, None
            for kk in range(len(idx) - 1):
                j = idx[kk]
                if h[j] > pdh:
                    side, epx = 1, o[idx[kk + 1]]; break
                if l[j] < pdl:
                    side, epx = -1, o[idx[kk + 1]]; break
            BRK.append((side * (cl - epx) * pv - cost) if side != 0 else None)
        Y = np.array(Y); GAP = np.array(GAP); DRV = np.array(DRV); RA = np.array(RA)
        agree = np.sign(GAP) == np.sign(DRV)
        # BIG drive (trailing-60 top tercile)
        big = np.zeros(len(DRV), bool)
        for i in range(len(DRV)):
            if i >= 60:
                big[i] = abs(DRV[i]) >= np.percentile(np.abs(DRV[i - 60:i]), 66.667)

        print("=" * 84)
        print(f"  {sym}  intraday combos off-open (LONG/SHORT $/1ct net 1t, n={len(Y)}) EXPLORATORY")
        print(f"  {'strategy':<20}{'mean$':>8}{'Sharpe':>8}{'win%':>6}{'worst$':>9}   era 10-15/16-19/20-21/22-26")
        drmom = np.sign(DRV) * RA * pv - cost
        summ("A agree-day mom", drmom[agree], Y[agree])
        summ("A disagree-day mom", drmom[~agree], Y[~agree])
        summ("A agree+BIG mom", drmom[agree & big], Y[agree & big])
        gapfade = -np.sign(GAP) * RA * pv - cost
        summ("A drive-confirms-fade", gapfade[~agree], Y[~agree])   # gap faded by drive
        brk = np.array([b for b in BRK if b is not None])
        brk_y = np.array([Y[i] for i, b in enumerate(BRK) if b is not None])
        summ("B prior-day breakout", brk, brk_y)
        summ("B prior-day fade", -brk - 2 * cost + cost, brk_y)     # mirror (approx costs)
        print()


if __name__ == "__main__":
    main()
