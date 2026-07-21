"""Round 29 — Macro-announcement premium (ES). Frozen spec registered in
HYPOTHESES.md (commit 6cd3090), run on the SEARCH set only.

  * Announcement day = date in {FOMC, CPI, NFP} per oos/data/econ_calendar.csv.
  * LONG ES at RTH 09:30 open, exit 15:55 flatten. One trade/day, flat by close.
  * SEARCH 2010-06..2025-06-04; HOLDOUT 2025-06-05..2026-06-05 = LOCKED (skipped).
  * Non-announcement RTH-long = baseline diagnostic.
  * Net 1-tick (primary) + 2-tick; seed 7 bootstrap; deflated Sharpe N=32.

Usage: .venv/bin/python oos/round29_announcement_premium.py
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

RTH_OPEN, FLATTEN = 9 * 60 + 30, 15 * 60 + 55
HOLDOUT_START = date(2025, 6, 5)
N_TRIALS = 32
ANN_EVENTS = {"FOMC", "CPI", "NFP"}


def announcement_days() -> set[date]:
    out = set()
    with (HERE / "data" / "econ_calendar.csv").open() as f:
        for row in csv.DictReader(f):
            if row["event"] in ANN_EVENTS:
                try:
                    out.add(date.fromisoformat(row["date"]))
                except ValueError:
                    continue
    return out


def rth_sessions(ts):
    by_day = defaultdict(list)
    for i, t in enumerate(ts):
        if t.weekday() < 5 and RTH_OPEN <= C.mins(t) <= FLATTEN:
            by_day[t.date()].append(i)
    return by_day


def deflated_sharpe(net: np.ndarray):
    n = len(net)
    mu, sd = net.mean(), net.std(ddof=1)
    if sd <= 0 or n < 3:
        return float("nan"), float("nan"), float("nan"), float("nan")
    SR = mu / sd
    z = (net - mu) / sd
    skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
    se = math.sqrt(max((1 - skew * SR + ((kurt - 1) / 4) * SR ** 2) / (n - 1), 1e-18))
    emc = 0.5772156649015329
    Nd = NormalDist()
    SR0 = se * ((1 - emc) * Nd.inv_cdf(1 - 1.0 / N_TRIALS)
                + emc * Nd.inv_cdf(1 - 1.0 / (N_TRIALS * math.e)))
    return SR, SR0, SR - SR0, Nd.cdf((SR - SR0) / se)


def build(sym, ann: set[date], want_ann: bool):
    ts, o, h, l, c, v = C.load(sym)
    by_day = rth_sessions(ts)
    trades = []
    for d in sorted(by_day):
        if d >= HOLDOUT_START:
            continue
        if (d in ann) != want_ann:
            continue
        idxs = by_day[d]
        ei, xi = idxs[0], idxs[-1]
        trades.append((ei, xi, o[ei], c[xi], +1))   # LONG open -> close
    return ts, trades


def netarr(sym, trades, slip=1):
    spec = C.SPECS[sym]
    cost = spec["comm_rt"] + 2 * slip * spec["tick"] * spec["pt"]
    return np.array([(xp - ep) * side * spec["pt"] - cost for _, _, ep, xp, side in trades])


def report(sym, label, trades, ts, with_dsr=False):
    for slip in (1, 2):
        C.SLIP_TICKS = slip
        cell = C.evaluate(trades, ts, sym)
        print(f"  [{label:<22} {slip}t] n={cell.get('n')} avg=${cell.get('avg_usd')} "
              f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
              f"boot={cell.get('p_bootstrap')} yrs+={cell.get('pct_years_positive')}%")
    C.SLIP_TICKS = 1
    if with_dsr and trades:
        SR, SR0, hair, dsr = deflated_sharpe(netarr(sym, trades))
        print(f"       Sharpe={SR:.4f} SR0(N={N_TRIALS})={SR0:.4f} "
              f"deflated={hair:+.4f} DSR={dsr:.3f}")
    return netarr(sym, trades) if trades else np.array([])


def main():
    ann = announcement_days()
    print("=" * 80)
    print("  ROUND 29 — Macro-announcement premium (ES) — SEARCH SET ONLY")
    print(f"  announcement set {sorted(ANN_EVENTS)}; {len(ann)} calendar days total")
    print("  (holdout 2025-06-05..2026-06-05 LOCKED, not evaluated)")
    print("=" * 80)
    for sym in ("ES", "MES"):
        print(f"\n--- {sym} ---")
        ts, at = build(sym, ann, want_ann=True)
        _, bt = build(sym, ann, want_ann=False)
        an = report(sym, "ANNOUNCEMENT long", at, ts, with_dsr=True)
        bn = report(sym, "non-announce (base)", bt, ts)
        if an.size and bn.size:
            print(f"       ANN ${an.mean():+.2f}/trade vs baseline ${bn.mean():+.2f}  "
                  f"(differential ${an.mean()-bn.mean():+.2f})")
    print("\nPASS (ES ANNOUNCEMENT 1t, SEARCH): n>=200, PF>=1.15, p<0.05 (t AND boot),")
    print("yrs+>=60%, deflated>0. Any fail -> KILL, holdout stays locked.")


if __name__ == "__main__":
    main()
