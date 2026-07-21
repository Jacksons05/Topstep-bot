"""DESCRIPTIVE tail-guard check for the overnight drift (NOT a validation; holdout
spent). Question: does an A-PRIORI 'skip overnight when prior VIX > 30' rule remove
the crisis-gap tail, and at what cost to the drift? Threshold 30 is pre-specified
(standard 'elevated VIX'), not tuned. Output informs a FORWARD paper-log design.

Usage: .venv/bin/python oos/overnight_tail_guard.py
"""
from __future__ import annotations

import bisect
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402
from overnight_characterization import load_vix  # noqa: E402

EVE, OPEN = 18 * 60, 9 * 60 + 30
VIX_CEIL = 30.0   # a-priori 'elevated' threshold, NOT tuned


def nights(sym):
    ts, o, h, l, c, v = C.load(sym)
    by_date = defaultdict(list)
    for i, t in enumerate(ts):
        by_date[t.date()].append(i)
    spec = C.SPECS[sym]
    cost = spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
    out = []  # (D, pnl_usd, priorVIX)
    for E in sorted(by_date):
        if E.weekday() not in (6, 0, 1, 2, 3):
            continue
        D = E + timedelta(days=1)
        if D not in by_date:
            continue
        e18 = next((i for i in by_date[E] if C.mins(ts[i]) >= EVE), None)
        d0930 = next((i for i in by_date[D] if C.mins(ts[i]) >= OPEN), None)
        if e18 is None or d0930 is None:
            continue
        pnl = (o[d0930] - o[e18]) * spec["pt"] - cost
        out.append((D, pnl))
    return out


def main():
    vix = load_vix()
    vdays = sorted(vix)

    def pvix(d):
        i = bisect.bisect_left(vdays, d) - 1
        return vix[vdays[i]] if i >= 0 else np.nan

    for sym in ("MES", "MNQ"):
        rows = nights(sym)
        D = np.array([r[0] for r in rows])
        pnl = np.array([r[1] for r in rows])
        pv = np.array([pvix(d) for d in D])
        guard = pv <= VIX_CEIL          # KEEP only these nights (skip VIX>30)
        print("=" * 74)
        print(f"  {sym} — overnight drift, a-priori tail guard (skip prior VIX > {VIX_CEIL:.0f})")
        print(f"  DESCRIPTIVE / forward-test design only -- NOT a validated result.")
        print("=" * 74)
        for name, mask in (("ALL nights", np.ones(len(rows), bool)), ("GUARDED (VIX<=30)", guard)):
            p = pnl[mask]
            print(f"  [{name:<18}] n={p.size}  mean=${p.mean():+.1f}  worst=${p.min():,.0f}  "
                  f"P(loss>$500)={100*(p<-500).mean():.1f}%  P(loss>$1000)={100*(p<-1000).mean():.1f}%")
        skipped = (~guard).sum()
        print(f"  guard skips {skipped} nights ({100*skipped/len(rows):.0f}%); "
              f"drift retained on kept nights vs all: "
              f"${pnl[guard].mean():+.1f} vs ${pnl.mean():+.1f}")
        # does it matter which regime? mean pnl on the SKIPPED (high-VIX) nights, by era
        def era(d):
            return "<=2021" if d.year <= 2021 else "2022-26"
        for e in ("<=2021", "2022-26"):
            m = np.array([era(d) == e for d in D]) & (~guard)
            if m.any():
                print(f"    skipped(high-VIX) nights {e}: mean=${pnl[m].mean():+.1f} n={m.sum()} "
                      f"(pre-2022 these carried the premium; post-2022 they don't)")
        print()


if __name__ == "__main__":
    main()
