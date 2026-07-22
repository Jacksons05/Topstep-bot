"""MAX-EFFORT EXPLORATORY (holdout spent -> hypothesis generation, NOT validation) sweep
of CONDITIONAL/reactive intraday-off-open strategies. Unconditional RTH long is dead
(intraday_subwindow_explore.py); test whether a signal derived FROM the open helps:

  gap   = open(09:30) - prior RTH close(16:00)     [the overnight gap]
  drive = price(10:00) - open(09:30)               [the opening 30-min drive]

Strategies (all LONG/SHORT, net 1-tick, one trade/day, flat by close):
  1 gap-fade      side=-sign(gap),   open->close
  2 gap-continue  side=+sign(gap),   open->close
  3 drive-mom     side=+sign(drive), 10:00->close
  4 drive-revert  side=-sign(drive), 10:00->close
  5 gap-fade BIG   only |gap|   in top tercile of trailing 60, side=-sign(gap)
  6 drive-mom BIG  only |drive| in top tercile of trailing 60, side=+sign(drive)

Reports mean$/Sharpe/win/worst + by-era. If all are dead, the intraday-off-open space
is closed. Any positive one is a FORWARD-TEST candidate only (holdout spent).

Usage: .venv/bin/python oos/intraday_conditional_explore.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

OPEN, T1000, CLOSE = 9 * 60 + 30, 10 * 60, 16 * 60


def era(y):
    return "2010-15" if y <= 2015 else "2016-19" if y <= 2019 else "2020-21" if y <= 2021 else "2022-26"


def px(ts, arr, idxs, m, use_close=False):
    for j in idxs:
        if C.mins(ts[j]) >= m:
            return arr[j]
    return None


def summarize(name, pnl, yr):
    if len(pnl) < 10:
        print(f"  {name:<16} (n={len(pnl)} too few)")
        return
    byera = []
    for e in ("2010-15", "2016-19", "2020-21", "2022-26"):
        mm = np.array([era(y) == e for y in yr])
        byera.append(f"{pnl[mm].mean():+.0f}" if mm.any() else "--")
    print(f"  {name:<16}{pnl.mean():>+8.1f}{pnl.mean()/pnl.std():>8.3f}"
          f"{100*(pnl>0).mean():>6.0f}%{pnl.min():>9,.0f}   {'/'.join(byera)}")


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
        recs = []   # (year, gap, drive, ret_full, ret_after1000)
        for k, d in enumerate(days):
            if k == 0:
                continue
            pidx, idx = by_date[days[k - 1]], by_date[d]
            prior_close = c[pidx[-1]]
            op = px(ts, o, idx, OPEN)
            p10 = px(ts, o, idx, T1000)
            cl = c[idx[-1]]
            if None in (op, p10) or prior_close is None:
                continue
            recs.append((d.year, op - prior_close, p10 - op,
                         (cl - op), (cl - p10)))
        yr = np.array([r[0] for r in recs])
        gap = np.array([r[1] for r in recs])
        drive = np.array([r[2] for r in recs])
        rf = np.array([r[3] for r in recs])       # open->close move (points)
        ra = np.array([r[4] for r in recs])       # 10:00->close move (points)

        # trailing-60 top-tercile |gap| / |drive| (causal)
        def big_mask(x):
            m = np.zeros(len(x), bool)
            for i in range(len(x)):
                if i >= 60:
                    thr = np.percentile(np.abs(x[i - 60:i]), 66.667)
                    m[i] = abs(x[i]) >= thr
            return m

        print("=" * 82)
        print(f"  {sym}  intraday CONDITIONAL off-open (LONG/SHORT, $/1ct net 1t, n={len(recs)}) EXPLORATORY")
        print(f"  {'strategy':<16}{'mean$':>8}{'Sharpe':>8}{'win%':>6}{'worst$':>9}"
              f"   era 10-15/16-19/20-21/22-26")
        summarize("1 gap-fade", -np.sign(gap) * rf * pv - cost, yr)
        summarize("2 gap-continue", np.sign(gap) * rf * pv - cost, yr)
        summarize("3 drive-mom", np.sign(drive) * ra * pv - cost, yr)
        summarize("4 drive-revert", -np.sign(drive) * ra * pv - cost, yr)
        bg, bd = big_mask(gap), big_mask(drive)
        summarize("5 gap-fade BIG", (-np.sign(gap) * rf * pv - cost)[bg], yr[bg])
        summarize("6 drive-mom BIG", (np.sign(drive) * ra * pv - cost)[bd], yr[bd])
        print()


if __name__ == "__main__":
    main()
