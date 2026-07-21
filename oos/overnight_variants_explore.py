"""EXPLORATORY (hypothesis generation; holdout spent -> nothing here is validated).
Deep dive on overnight variants for the forward log:
  1. FULL 18:00->09:30 vs EVENING 18:00->06:00 (drop the weak/negative pre-open),
     split by era -> did the drift survive the 2022 decay?
  2. Tail per variant (does the shorter window keep the worst night under the $1k DLL?)
  3. Day-of-week robustness by era (is 'Wednesday strongest' real or noise?)

Usage: .venv/bin/python oos/overnight_variants_explore.py
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
            by_date[t.date()].append(i)
        spec = C.SPECS[sym]
        cost = spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
        recs = []
        for E in sorted(by_date):
            if E.weekday() not in (6, 0, 1, 2, 3):
                continue
            D = E + timedelta(days=1)
            if D not in by_date:
                continue
            p18 = px(ts, o, by_date[E], 18 * 60)
            p06 = px(ts, o, by_date[D], 6 * 60)
            p0930 = px(ts, o, by_date[D], 9 * 60 + 30)
            if None in (p18, p06, p0930):
                continue
            full = (p0930 - p18) * spec["pt"] - cost
            evening = (p06 - p18) * spec["pt"] - cost
            recs.append((D, D.weekday(), full, evening))
        Dw = np.array([r[1] for r in recs])
        yr = np.array([r[0].year for r in recs])
        full = np.array([r[2] for r in recs])
        eve = np.array([r[3] for r in recs])
        print("=" * 78)
        print(f"  {sym}  overnight variants (n={len(recs)}, $/1 contract net 1t)  EXPLORATORY")
        print("=" * 78)
        for label, arr in (("FULL 18->0930", full), ("EVENING 18->06", eve)):
            print(f"  [{label}]  all: mean=${arr.mean():+.1f} Sharpe={arr.mean()/arr.std():.3f} "
                  f"win={100*(arr>0).mean():.0f}% worst=${arr.min():,.0f} "
                  f"P(>-500)={100*(arr<-500).mean():.1f}% P(>-1000)={100*(arr<-1000).mean():.1f}%")
            for e in ("2010-15", "2016-19", "2020-21", "2022-26"):
                m = np.array([era(y) == e for y in yr])
                if m.any():
                    a = arr[m]
                    print(f"       {e}: mean=${a.mean():+6.1f}  Sharpe={a.mean()/a.std():+.3f}  "
                          f"win={100*(a>0).mean():.0f}%  worst=${a.min():,.0f}  n={m.sum()}")
        # day-of-week robustness on FULL, per era (is Wed real?)
        print("  [day-of-week x era, FULL overnight mean$] (Mon=weekend)")
        hdr = "     " + "".join(f"{d:>9}" for d in ("Mon", "Tue", "Wed", "Thu", "Fri"))
        print(hdr)
        for e in ("2010-15", "2016-19", "2020-21", "2022-26"):
            row = f"  {e}"
            for dow in range(5):
                m = (Dw == dow) & np.array([era(y) == e for y in yr])
                row += f"{full[m].mean():>9.0f}" if m.any() else f"{'--':>9}"
            print(row)
        print()


if __name__ == "__main__":
    main()
