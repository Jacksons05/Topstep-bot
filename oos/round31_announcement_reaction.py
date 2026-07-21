"""Round 31 — 8:30 ET pre-open announcement-reaction drift (ES). Frozen spec
registered in HYPOTHESES.md (commit 1b36c86), run on the SEARCH set only.

  * Announcement day = {CPI, NFP} per econ_calendar.csv.
  * reaction = sign(open[08:45] - open[08:30]); enter 08:45 IN that direction
    (continuation), exit 09:30 RTH open. One trade/day, flat by 09:30 (tail-safe).
  * SEARCH 2010-06..2025-06-04; HOLDOUT 2025-06-05..2026-06-05 LOCKED (skipped).
  * Net 1-tick + 2-tick; seed 7; deflated Sharpe N=34. Mirror + non-announcement
    baseline = diagnostics.

Usage: .venv/bin/python oos/round31_announcement_reaction.py
"""
from __future__ import annotations

import csv
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import NormalDist

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

M0830, M0845, M0930 = 8 * 60 + 30, 8 * 60 + 45, 9 * 60 + 30
HOLDOUT_START = date(2025, 6, 5)
N_TRIALS = 34
ANN = {"CPI", "NFP"}


def ann_days():
    out = set()
    with (HERE / "data" / "econ_calendar.csv").open() as f:
        for row in csv.DictReader(f):
            if row["event"] in ANN:
                try:
                    out.add(date.fromisoformat(row["date"]))
                except ValueError:
                    pass
    return out


def deflated_sharpe(net):
    n = len(net)
    mu, sd = net.mean(), net.std(ddof=1)
    if sd <= 0 or n < 3:
        return (float("nan"),) * 4
    SR = mu / sd
    z = (net - mu) / sd
    skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
    se = math.sqrt(max((1 - skew * SR + ((kurt - 1) / 4) * SR ** 2) / (n - 1), 1e-18))
    emc = 0.5772156649015329
    Nd = NormalDist()
    SR0 = se * ((1 - emc) * Nd.inv_cdf(1 - 1.0 / N_TRIALS)
                + emc * Nd.inv_cdf(1 - 1.0 / (N_TRIALS * math.e)))
    return SR, SR0, SR - SR0, Nd.cdf((SR - SR0) / se)


def bar_at(ts, idxs, minute):
    return next((i for i in idxs if C.mins(ts[i]) >= minute), None)


def build(sym, ann, want_ann, mirror=False):
    ts, o, h, l, c, v = C.load(sym)
    by_date = defaultdict(list)
    for i, t in enumerate(ts):
        by_date[t.date()].append(i)
    trades = []
    for d in sorted(by_date):
        if d >= HOLDOUT_START or ((d in ann) != want_ann):
            continue
        idxs = by_date[d]
        b830, b845, b930 = bar_at(ts, idxs, M0830), bar_at(ts, idxs, M0845), bar_at(ts, idxs, M0930)
        if None in (b830, b845, b930) or not (b830 < b845 < b930):
            continue
        react = o[b845] - o[b830]
        if react == 0:
            continue
        side = (1 if react > 0 else -1)
        if mirror:
            side = -side
        trades.append((b845, b930, o[b845], o[b930], side))
    return ts, trades


def netarr(sym, trades, slip=1):
    spec = C.SPECS[sym]
    cost = spec["comm_rt"] + 2 * slip * spec["tick"] * spec["pt"]
    return np.array([(xp - ep) * side * spec["pt"] - cost for _, _, ep, xp, side in trades])


def show(sym, label, trades, ts, dsr=False):
    for slip in (1, 2):
        C.SLIP_TICKS = slip
        cell = C.evaluate(trades, ts, sym)
        print(f"  [{label:<22} {slip}t] n={cell.get('n')} avg=${cell.get('avg_usd')} "
              f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
              f"boot={cell.get('p_bootstrap')} yrs+={cell.get('pct_years_positive')}%")
    C.SLIP_TICKS = 1
    if dsr and trades:
        SR, SR0, hair, d = deflated_sharpe(netarr(sym, trades))
        print(f"       Sharpe={SR:.4f} SR0(N={N_TRIALS})={SR0:.4f} deflated={hair:+.4f} DSR={d:.3f}")


def main():
    ann = ann_days()
    print("=" * 80)
    print(f"  ROUND 31 — 8:30 pre-open announcement-reaction (ES) — SEARCH ONLY")
    print(f"  {sorted(ANN)}; enter 08:45 in reaction dir, exit 09:30. (holdout LOCKED)")
    print("=" * 80)
    for sym in ("ES", "MES"):
        print(f"\n--- {sym} ---")
        ts, at = build(sym, ann, True)
        show(sym, "ANNOUNCE continuation", at, ts, dsr=True)
        _, mt = build(sym, ann, True, mirror=True)
        show(sym, "MIRROR (reversion)", mt, ts)
        _, bt = build(sym, ann, False)
        show(sym, "non-announce baseline", bt, ts)
    print("\nPASS (ES ANNOUNCE 1t, SEARCH): n>=200, PF>=1.15, p<0.05 (t AND boot),")
    print("yrs+>=60%, deflated>0. Any fail -> KILL, holdout locked.")


if __name__ == "__main__":
    main()
