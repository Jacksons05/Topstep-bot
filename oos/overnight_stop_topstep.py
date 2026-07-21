"""EXPLORATORY (holdout spent; forward-test design, NOT validation): can a STOP-LOSS
make the EVENING overnight drift (18:00->06:00 ET) survive TopstepX's $1k DLL / $2k
trailing MLL? The overnight session trades CONTINUOUSLY (no intraday gap), so a stop
catches a grinding decline -- EXCEPT on limit-lock/fast nights where the fill is worse.

Levers tested (a-priori, not tuned): stop in {none, $300, $500} per contract, on MES
(micro, tail ~1/10 of ES) and MNQ. Walks 5-min bars; stop fills at the stop price, or
at the bar OPEN if the bar gapped through it (poor-fill model). Reports the resulting
mean / worst-night / stop-trigger rate / by-era, plus 'gap-through' nights (fill worse
than the stop) which are the residual un-stoppable tail.

Usage: .venv/bin/python oos/overnight_stop_topstep.py
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

ENTRY_MIN, EXIT_MIN = 18 * 60, 6 * 60   # 18:00 ET -> 06:00 ET (evening slice)


def era(y):
    return "2010-15" if y <= 2015 else "2016-19" if y <= 2019 else "2020-21" if y <= 2021 else "2022-26"


def simulate(sym, stop_usd):
    """Return per-night (year, pnl_usd, stopped, gapped) for the evening drift with
    an optional dollar stop (0 = no stop)."""
    ts, o, h, l, c, v = C.load(sym)
    by_date = defaultdict(list)
    for i, t in enumerate(ts):
        by_date[t.date()].append(i)
    spec = C.SPECS[sym]
    pt, cost = spec["pt"], spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
    out = []
    for E in sorted(by_date):
        if E.weekday() not in (6, 0, 1, 2, 3):
            continue
        D = E + timedelta(days=1)
        if D not in by_date:
            continue
        # entry bar 18:00 on E; hold through to 06:00 on D
        ei = next((i for i in by_date[E] if C.mins(ts[i]) >= ENTRY_MIN), None)
        if ei is None:
            continue
        # ordered hold bars: E's 18:00.. end of E, then D's start.. 06:00
        hold = [i for i in by_date[E] if i > ei] + \
               [i for i in by_date[D] if C.mins(ts[i]) < EXIT_MIN]
        xi = next((i for i in by_date[D] if C.mins(ts[i]) >= EXIT_MIN), None)
        if xi is None:
            continue
        entry = o[ei]
        stop_px = entry - stop_usd / pt if stop_usd > 0 else -1e18
        pnl, stopped, gapped = None, False, False
        if stop_usd > 0:
            for i in hold:
                if l[i] <= stop_px:
                    fill = min(o[i], stop_px)   # gap through -> fill at bar open
                    if o[i] < stop_px:
                        gapped = True
                    pnl = (fill - entry) * pt - cost
                    stopped = True
                    break
        if pnl is None:
            pnl = (o[xi] - entry) * pt - cost
        out.append((D.year, pnl, stopped, gapped))
    return out


def summarize(sym, stop_usd, rows):
    pnl = np.array([r[1] for r in rows])
    yr = np.array([r[0] for r in rows])
    stopped = np.array([r[2] for r in rows])
    gapped = np.array([r[3] for r in rows])
    tag = f"stop=${stop_usd}" if stop_usd else "no-stop"
    print(f"  [{tag:<10}] n={len(rows)} mean=${pnl.mean():+.1f} Sharpe={pnl.mean()/pnl.std():.3f} "
          f"win={100*(pnl>0).mean():.0f}% worst=${pnl.min():,.0f}  "
          f"stopped={100*stopped.mean():.0f}% gap-thru={gapped.sum()} "
          f"P(loss>$1000)={100*(pnl<-1000).mean():.1f}%")
    # recent era detail
    for e in ("2020-21", "2022-26"):
        m = np.array([era(y) == e for y in yr])
        if m.any():
            a = pnl[m]
            print(f"       {e}: mean=${a.mean():+.1f} worst=${a.min():,.0f} n={m.sum()}")


def main():
    for sym in ("MES", "MNQ"):
        print("=" * 78)
        print(f"  {sym}  EVENING drift (18:00->06:00 ET) with stop-loss  --  TopstepX fit (EXPLORATORY)")
        print(f"  DLL=$1,000  trailing MLL=$2,000  -- want worst-night well under $1k")
        print("=" * 78)
        for stop in (0, 500, 300):
            summarize(sym, stop, simulate(sym, stop))
        print()


if __name__ == "__main__":
    main()
