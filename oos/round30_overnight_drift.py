"""Round 30 — Overnight drift, Topstep-LEGAL window (ES). Frozen spec registered
in HYPOTHESES.md (commit 6b18700), run on the SEARCH set only.

  * LONG ES. Enter first 5-min bar open >= 18:00 ET (evening session reopen).
    Exit first bar open >= 09:30 ET the next RTH morning. One trade/session,
    Sun-Thu evening -> Mon-Fri morning. Flat by 09:30 ET.
  * SEARCH exit-date 2010-06..2025-06-04; HOLDOUT 2025-06-05..2026-06-05 LOCKED.
  * Net 1-tick (primary) + 2-tick; seed 7; deflated Sharpe N=33.
  * Reports the overnight P&L TAIL (worst / 1st-pct per contract) as a Topstep
    risk flag; if the edge bar clears, the eval-pass MC is the required 2nd gate.

Usage: .venv/bin/python oos/round30_overnight_drift.py
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from statistics import NormalDist

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

EVE_MIN, RTH_OPEN_MIN = 18 * 60, 9 * 60 + 30
HOLDOUT_START = date(2025, 6, 5)
N_TRIALS = 33


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


def build(sym):
    ts, o, h, l, c, v = C.load(sym)
    by_date = defaultdict(list)
    for i, t in enumerate(ts):
        by_date[t.date()].append(i)
    trades = []
    for E in sorted(by_date):
        if E.weekday() not in (6, 0, 1, 2, 3):    # evening opens Sun..Thu
            continue
        D = E + timedelta(days=1)                  # RTH morning
        if D not in by_date or D >= HOLDOUT_START:
            continue
        entry_i = next((i for i in by_date[E] if C.mins(ts[i]) >= EVE_MIN), None)
        exit_i = next((i for i in by_date[D] if C.mins(ts[i]) >= RTH_OPEN_MIN), None)
        if entry_i is None or exit_i is None:
            continue
        trades.append((entry_i, exit_i, o[entry_i], o[exit_i], +1))
    return ts, trades


def netarr(sym, trades, slip=1):
    spec = C.SPECS[sym]
    cost = spec["comm_rt"] + 2 * slip * spec["tick"] * spec["pt"]
    return np.array([(xp - ep) * side * spec["pt"] - cost for _, _, ep, xp, side in trades])


def main():
    print("=" * 80)
    print("  ROUND 30 — Overnight drift, Topstep-LEGAL window (ES) — SEARCH ONLY")
    print("  LONG 18:00 ET -> 09:30 ET next RTH open. (holdout LOCKED, not evaluated)")
    print("=" * 80)
    for sym in ("ES", "MES", "MNQ"):
        ts, tr = build(sym)
        print(f"\n--- {sym} --- (n={len(tr)} overnight sessions)")
        for slip in (1, 2):
            C.SLIP_TICKS = slip
            cell = C.evaluate(tr, ts, sym)
            print(f"  [{slip}-tick] n={cell.get('n')} avg=${cell.get('avg_usd')} "
                  f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
                  f"boot={cell.get('p_bootstrap')} yrs+={cell.get('pct_years_positive')}%")
        C.SLIP_TICKS = 1
        net = netarr(sym, tr, 1)
        SR, SR0, hair, dsr = deflated_sharpe(net)
        print(f"       Sharpe={SR:.4f} SR0(N={N_TRIALS})={SR0:.4f} deflated={hair:+.4f} DSR={dsr:.3f}")
        # overnight tail (Topstep-risk flag, per 1 contract)
        print(f"       TAIL/contract: worst=${net.min():,.0f}  1st-pct=${np.percentile(net,1):,.0f} "
              f" 5th-pct=${np.percentile(net,5):,.0f}  |  P(loss>$500)={100*(net<-500).mean():.1f}%"
              f"  P(loss>$1000)={100*(net<-1000).mean():.1f}%")
    print("\nPASS bar (ES 1-tick, SEARCH): n>=200, PF>=1.15, p<0.05 (t AND boot),")
    print("yrs+>=60%, deflated>0. If it clears -> eval-pass MC vs $1k DLL/$2k MLL is")
    print("the required 2nd gate. Any edge-bar fail -> KILL, holdout locked.")


if __name__ == "__main__":
    main()
