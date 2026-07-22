"""Track 1 (fork-C first pass, EXPLORATORY; holdout spent): does the DAILY net-GEX sign
condition the opening-drive momentum? Coarse proxy for the intraday-0DTE-gamma mechanism
(which is data-blocked -- UW retains no intraday history). If the daily shadow shows
nothing multi-era, the intraday version needs forward capture to even be worth it.

Mechanism: negative net-GEX = dealers SHORT gamma -> hedge WITH the move -> amplify ->
opening drive CONTINUES (momentum). Positive net-GEX = dealers LONG gamma -> dampen ->
drive REVERTS. Causal: SqueezeMetrics GEX is an EOD reading, so day D's open is governed
by the PRIOR trading day's GEX (gex_regime_for_session convention).

  drive = open(10:00) - open(09:30);  trade 10:00->close; net 1-tick.
  neg-GEX day -> side=sign(drive) (momentum);  pos-GEX day -> side=-sign(drive) (reversion).
SqueezeMetrics GEX is SPX-based -> cleanest on ES/MES; MNQ is a proxy.

Usage: .venv/bin/python oos/gex_drive_test.py
"""
from __future__ import annotations

import bisect
import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

OPEN, T1000, CLOSE = 9 * 60 + 30, 10 * 60, 16 * 60


def era(y):
    return "2011-15" if y <= 2015 else "2016-19" if y <= 2019 else "2020-21" if y <= 2021 else "2022-26"


def load_gex():
    out = {}
    with (HERE / "data" / "squeeze_dix_gex.csv").open() as f:
        for row in csv.DictReader(f):
            try:
                out[date.fromisoformat(row["date"])] = float(row["gex"])
            except (ValueError, KeyError):
                pass
    return out


def _px(ts, arr, idxs, m):
    for j in idxs:
        if C.mins(ts[j]) >= m:
            return arr[j]
    return None


def summ(name, pnl, yr):
    pnl = np.asarray(pnl, float)
    if len(pnl) < 30:
        print(f"  {name:<22} n={len(pnl)} (too few)"); return
    be = []
    for e in ("2011-15", "2016-19", "2020-21", "2022-26"):
        mm = np.array([era(y) == e for y in yr])
        be.append(f"{pnl[mm].mean():+.0f}" if mm.any() else "--")
    npos = sum(1 for x in be if x != "--" and float(x) > 0)
    t = pnl.mean() / (pnl.std() / np.sqrt(len(pnl))) if pnl.std() > 0 else 0
    flag = "  <== MULTI-ERA+" if npos >= 3 and pnl.mean() > 0 else ""
    print(f"  {name:<22}{pnl.mean():>+8.1f}{pnl.mean()/pnl.std():>8.3f}{t:>7.2f}"
          f"{100*(pnl>0).mean():>6.0f}%{pnl.min():>9,.0f}   {'/'.join(be)}{flag}")


def prior_gex(gdays, gex, d):
    i = bisect.bisect_left(gdays, d) - 1
    return gex[gdays[i]] if i >= 0 else None


def main():
    gex = load_gex()
    gdays = sorted(gex)
    print(f"GEX: {len(gex)} daily rows {gdays[0]}..{gdays[-1]} | "
          f"share negative = {100*np.mean([v < 0 for v in gex.values()]):.0f}%")
    for sym in ("ES", "MES", "MNQ"):
        ts, o, h, l, c, v = C.load(sym)
        by_date = defaultdict(list)
        for i, t in enumerate(ts):
            if t.weekday() < 5 and OPEN <= C.mins(t) <= CLOSE:
                by_date[t.date()].append(i)
        spec = C.SPECS[sym]
        cost = spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
        pv = spec["pt"]
        cond, condy = [], []       # gamma-conditioned combined
        negm, negmy = [], []       # neg-GEX momentum leg
        posr, posry = [], []       # pos-GEX reversion leg
        mir, miry = [], []         # mirror (no directional edge check)
        for d in sorted(by_date):
            pg = prior_gex(gdays, gex, d)
            if pg is None:
                continue
            idx = by_date[d]
            op, p10 = _px(ts, o, idx, OPEN), _px(ts, o, idx, T1000)
            cl = c[idx[-1]]
            if op is None or p10 is None:
                continue
            drive = np.sign(p10 - op)
            if drive == 0:
                continue
            ra = (cl - p10) * pv
            side = drive if pg < 0 else -drive     # neg->mom, pos->revert
            pnl = side * ra - cost
            cond.append(pnl); condy.append(d.year)
            mir.append(-side * ra - cost); miry.append(d.year)
            if pg < 0:
                negm.append(drive * ra - cost); negmy.append(d.year)
            else:
                posr.append(-drive * ra - cost); posry.append(d.year)
        print("=" * 88)
        print(f"  {sym}  daily-GEX-conditioned opening-drive ($/1ct net 1t)  EXPLORATORY")
        print(f"  {'stream':<22}{'mean$':>8}{'Sharpe':>8}{'t':>7}{'win%':>6}{'worst$':>9}"
              f"   era 11-15/16-19/20-21/22-26")
        summ("CONDITIONED (combo)", cond, condy)
        summ("neg-GEX momentum", negm, negmy)
        summ("pos-GEX reversion", posr, posry)
        summ("MIRROR (diag)", mir, miry)
        print()


if __name__ == "__main__":
    main()
