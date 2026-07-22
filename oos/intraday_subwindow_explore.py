"""EXPLORATORY (holdout spent -> hypothesis generation, NOT validation): does the
intraday RTH drift concentrate in a window OFF THE OPEN (analogous to how the overnight
drift lives in the evening slice)? An intraday trade is flat by the close -> NO overnight
gap tail, so if a window carries a cost-clearing drift it fits TopstepX far more easily
than the overnight strategy.

RTH windows (ET open->open, LONG, net 1-tick): 0930-1000, 0930-1030 (open hour),
1030-1400 (midday), 1400-1600 (afternoon), 1500-1600 (power hour), 0930-1600 (full).
Reports mean$/Sharpe/win/worst per window + by era (did it survive 2022-26?).

Usage: .venv/bin/python oos/intraday_subwindow_explore.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

# window name -> (start_min_ET, end_min_ET)
WINS = {
    "0930-1000 open30": (9 * 60 + 30, 10 * 60),
    "0930-1030 openhr": (9 * 60 + 30, 10 * 60 + 30),
    "1030-1400 midday": (10 * 60 + 30, 14 * 60),
    "1400-1600 pm":     (14 * 60, 16 * 60),
    "1500-1600 powerhr": (15 * 60, 16 * 60),
    "0930-1600 fullRTH": (9 * 60 + 30, 16 * 60),
}


def era(y):
    return "2010-15" if y <= 2015 else "2016-19" if y <= 2019 else "2020-21" if y <= 2021 else "2022-26"


def px(ts, o, idxs, m):
    i = next((j for j in idxs if C.mins(ts[j]) >= m), None)
    return o[i] if i is not None else None


def main():
    for sym in ("ES", "MNQ"):
        ts, o, h, l, c, v = C.load(sym)
        by_date = defaultdict(list)
        for i, t in enumerate(ts):
            if t.weekday() < 5 and 9 * 60 + 30 <= C.mins(t) <= 16 * 60:
                by_date[t.date()].append(i)
        spec = C.SPECS[sym]
        cost = spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
        recs = {name: [] for name in WINS}
        years = []
        for d in sorted(by_date):
            idxs = by_date[d]
            row = {}
            good = True
            for name, (a, b) in WINS.items():
                pa, pb = px(ts, o, idxs, a), px(ts, o, idxs, b)
                if pa is None or pb is None:
                    good = False
                    break
                row[name] = (pb - pa) * spec["pt"] - cost   # LONG, net
            if good:
                for name in WINS:
                    recs[name].append((d.year, row[name]))
        print("=" * 82)
        print(f"  {sym}  intraday RTH window drift (LONG, $/1 contract net 1t) — EXPLORATORY")
        print(f"  n={len(recs['0930-1600 fullRTH'])} days")
        print("=" * 82)
        print(f"  {'window':<20}{'mean$':>8}{'Sharpe':>8}{'win%':>7}{'worst$':>9}"
              f"   by-era mean (10-15/16-19/20-21/22-26)")
        for name in WINS:
            arr = np.array([p for _, p in recs[name]])
            yr = np.array([y for y, _ in recs[name]])
            byera = []
            for e in ("2010-15", "2016-19", "2020-21", "2022-26"):
                m = np.array([era(y) == e for y in yr])
                byera.append(f"{arr[m].mean():+.0f}" if m.any() else "--")
            print(f"  {name:<20}{arr.mean():>+8.1f}{arr.mean()/arr.std():>8.3f}"
                  f"{100*(arr>0).mean():>6.0f}%{arr.min():>9,.0f}   "
                  f"{'/'.join(byera)}")
        print()


if __name__ == "__main__":
    main()
