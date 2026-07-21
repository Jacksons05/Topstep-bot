"""EXPLORATORY overnight decomposition (hypothesis generation ONLY — the holdout is
spent, so nothing here is 'validated'; promising windows become FORWARD-LOG tests).

Two questions for shrinking the gap tail that makes the overnight drift Topstep-
incompatible:
  1. WHERE inside 18:00->09:30 ET does the drift live? A shorter hold that keeps most
     of the drift carries a SMALLER overnight-gap tail.
  2. Day-of-week: is the weekend (Sun-night) drift different?

Sub-windows (ET open->open): W1 18:00->24:00 (evening/Asia), W2 00:00->06:00 (Asia/
EU-early), W3 06:00->09:30 (EU/US-pre-open), plus the full 18:00->09:30.

Usage: .venv/bin/python oos/overnight_subwindow_explore.py
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

# (name, start_min_on_E?, start_min, end_min)  — E=evening date, D=next day
# 18:00 is on E; 00:00/06:00/09:30 are on D.
SUBS = [
    ("W1 18-24", 18 * 60, 24 * 60),      # E 18:00 -> D 00:00
    ("W2 00-06", 0, 6 * 60),             # D 00:00 -> D 06:00
    ("W3 06-0930", 6 * 60, 9 * 60 + 30),  # D 06:00 -> D 09:30
    ("FULL 18-0930", 18 * 60, 9 * 60 + 30),
]


def px_at(ts, o, idxs, minute):
    i = next((j for j in idxs if C.mins(ts[j]) >= minute), None)
    return o[i] if i is not None else None


def main():
    for sym in ("ES", "MNQ"):
        ts, o, h, l, c, v = C.load(sym)
        by_date = defaultdict(list)
        for i, t in enumerate(ts):
            by_date[t.date()].append(i)
        spec = C.SPECS[sym]
        # collect per-session prices at 18:00(E), 00:00(D), 06:00(D), 09:30(D)
        recs = []  # (D, dow, p18, p00, p06, p0930)
        for E in sorted(by_date):
            if E.weekday() not in (6, 0, 1, 2, 3):
                continue
            D = E + timedelta(days=1)
            if D not in by_date:
                continue
            p18 = px_at(ts, o, by_date[E], 18 * 60)
            p00 = px_at(ts, o, by_date[D], 0)
            p06 = px_at(ts, o, by_date[D], 6 * 60)
            p0930 = px_at(ts, o, by_date[D], 9 * 60 + 30)
            if None in (p18, p00, p06, p0930):
                continue
            recs.append((D, D.weekday(), p18, p00, p06, p0930))
        arr = {k: np.array([r[i] for r in recs]) for i, k in
               enumerate(["D", "dow", "p18", "p00", "p06", "p0930"])}
        n = len(recs)
        print("=" * 76)
        print(f"  {sym}  overnight sub-window decomposition  (n={n})  [$ per 1 contract, net 1t]")
        print("=" * 76)
        cost = spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
        segs = {
            "W1 18-24": arr["p00"] - arr["p18"],
            "W2 00-06": arr["p06"] - arr["p00"],
            "W3 06-0930": arr["p0930"] - arr["p06"],
            "FULL 18-0930": arr["p0930"] - arr["p18"],
        }
        print(f"  {'window':<14}{'mean$':>9}{'/hr$':>8}{'Sharpe':>8}{'win%':>7}"
              f"{'worst$':>10}{'P(>-500)':>10}")
        hours = {"W1 18-24": 6, "W2 00-06": 6, "W3 06-0930": 3.5, "FULL 18-0930": 15.5}
        for name in ["W1 18-24", "W2 00-06", "W3 06-0930", "FULL 18-0930"]:
            d = segs[name] * spec["pt"] - cost
            sr = d.mean() / d.std() if d.std() > 0 else float("nan")
            print(f"  {name:<14}{d.mean():>9.1f}{d.mean()/hours[name]:>8.1f}{sr:>8.3f}"
                  f"{100*(d>0).mean():>6.0f}%{d.min():>10,.0f}{100*(d<-500).mean():>9.1f}%")
        # day-of-week on the FULL overnight
        print("  [day-of-week, FULL overnight] (Mon=weekend/Sun-night)")
        full = segs["FULL 18-0930"] * spec["pt"] - cost
        for dow, lab in [(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri")]:
            m = arr["dow"] == dow
            if m.any():
                dd = full[m]
                print(f"     {lab}: n={m.sum()} mean=${dd.mean():+.1f} win={100*(dd>0).mean():.0f}% "
                      f"worst=${dd.min():,.0f}")
        print()


if __name__ == "__main__":
    main()
