"""Overnight-drift forensics (descriptive; holdout spent). Two questions for fork-B:
  1. Is the HIGH-VIX overnight premium robust across eras, or a 2020-crash artifact?
  2. What/when are the worst overnight nights (the tail we must size for)?

Usage: .venv/bin/python oos/overnight_tail_forensics.py
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402
from overnight_characterization import segments, load_vix  # noqa: E402

import bisect


def era(d):
    y = d.year
    if y <= 2015:
        return "2010-15"
    if y <= 2019:
        return "2016-19"
    if y <= 2021:
        return "2020-21"
    return "2022-26"


def main():
    vix = load_vix()
    vdays = sorted(vix)

    def prior_vix(d):
        i = bisect.bisect_left(vdays, d) - 1
        return vix[vdays[i]] if i >= 0 else np.nan

    for sym in ("ES",):
        rows = segments(sym)
        D = np.array([r[0] for r in rows])
        on = np.array([r[1] for r in rows]) * 1e4  # bps
        pv = np.array([prior_vix(d) for d in D])
        hi = np.nanpercentile(pv, 66.67)
        himask = pv >= hi

        print("=" * 74)
        print(f"  {sym} — HIGH-VIX overnight premium by era (mean bps / sum bps / n)")
        print("=" * 74)
        by_era = defaultdict(list)
        for d, x, m in zip(D, on, himask):
            if m:
                by_era[era(d)].append(x)
        for e in ("2010-15", "2016-19", "2020-21", "2022-26"):
            v = np.array(by_era.get(e, [0]))
            print(f"  {e}: mean={v.mean():+.1f}bps  sum={v.sum():+.0f}bps  n={len(by_era.get(e,[]))} "
                  f" win={(v>0).mean()*100:.0f}%")
        # how much of the ALL-high-VIX sum is 2020-21?
        allsum = on[himask].sum()
        s2021 = sum(by_era.get("2020-21", []))
        print(f"  -> 2020-21 is {s2021/allsum*100:.0f}% of the entire high-VIX overnight sum")

        print("\n  WORST 12 overnight nights (date D = RTH-open morning, bps, prior VIX):")
        order = np.argsort(on)[:12]
        for i in order:
            print(f"    {D[i]}  {on[i]:+.0f}bps   priorVIX={pv[i]:.1f}")
        # tail clustering: fraction of worst-1% nights in high-VIX
        p1 = np.percentile(on, 1)
        worst = on <= p1
        print(f"\n  of the worst 1% nights ({worst.sum()}), "
              f"{(himask & worst).sum()/worst.sum()*100:.0f}% are in the HIGH-VIX regime "
              f"(base rate {himask.mean()*100:.0f}%).")


if __name__ == "__main__":
    main()
