"""Round 27 — Turn-of-the-month intraday drift (ES). Runs the FROZEN spec
registered in HYPOTHESES.md (commit e2fc6ec) on the SEARCH set only.

  * TOM window = last 1 trading day of month + first 3 trading days of next month.
  * LONG ES at RTH 09:30 open, exit RTH 15:55 close. One trade/day, flat by close.
  * SEARCH: 2010-06 .. 2025-06-04.  HOLDOUT 2025-06-05 .. 2026-06-05 = LOCKED,
    NOT touched here (this script filters it out entirely).
  * Net at 1-tick (primary) + 2-tick (robustness), $4 RT comm. seed 7 bootstrap.
  * PASS bar: n>=200, PF>=1.15, p<0.05 (t AND 20k boot), >=60% yrs+, deflated
    Sharpe (SR - SR0@N=30) > 0. Baseline (non-TOM long) reported for contrast:
    if TOM ~= baseline, the "edge" is generic long drift, not a TOM effect -> KILL.

Usage: .venv/bin/python oos/round27_turn_of_month.py
"""
from __future__ import annotations

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
HOLDOUT_START = date(2025, 6, 5)     # LOCKED — nothing at/after this is touched
N_TRIALS = 30                         # program trial count (26 rounds + 3 screens + this)


def rth_sessions(ts):
    by_day = defaultdict(list)
    for i, t in enumerate(ts):
        if t.weekday() < 5 and RTH_OPEN <= C.mins(t) <= FLATTEN:
            by_day[t.date()].append(i)
    return by_day


def tom_flags(days):
    """rank within month (1=first trading day) + is-last-trading-day-of-month."""
    by_month = defaultdict(list)
    for d in days:
        by_month[(d.year, d.month)].append(d)
    rank, is_last = {}, {}
    for ds in by_month.values():
        ds = sorted(ds)
        for i, d in enumerate(ds):
            rank[d] = i + 1
            is_last[d] = (i == len(ds) - 1)
    return rank, is_last


def deflated_sharpe(net: np.ndarray):
    """Return (SR_per_trade, SR0_benchmark, haircut_SR = SR-SR0, DSR_prob)."""
    n = len(net)
    mu, sd = net.mean(), net.std(ddof=1)
    if sd <= 0 or n < 3:
        return float("nan"), float("nan"), float("nan"), float("nan")
    SR = mu / sd
    z = (net - mu) / sd
    skew = float((z ** 3).mean())
    kurt = float((z ** 4).mean())          # normal = 3
    var = (1 - skew * SR + ((kurt - 1) / 4) * SR ** 2) / (n - 1)
    se = math.sqrt(max(var, 1e-18))
    emc = 0.5772156649015329
    Nd = NormalDist()
    SR0 = se * ((1 - emc) * Nd.inv_cdf(1 - 1.0 / N_TRIALS)
                + emc * Nd.inv_cdf(1 - 1.0 / (N_TRIALS * math.e)))
    dsr = Nd.cdf((SR - SR0) / se)
    return SR, SR0, SR - SR0, dsr


def build(sym, want_tom: bool):
    ts, o, h, l, c, v = C.load(sym)
    by_day = rth_sessions(ts)
    days = sorted(by_day)
    rank, is_last = tom_flags(days)
    trades = []
    for d in days:
        if d >= HOLDOUT_START:            # LOCKED holdout — never touched
            continue
        in_tom = (rank[d] <= 3) or is_last[d]
        if in_tom != want_tom:
            continue
        idxs = by_day[d]
        ei, xi = idxs[0], idxs[-1]
        trades.append((ei, xi, o[ei], c[xi], +1))   # long open -> close
    return ts, trades


def report(sym, label, trades, ts):
    net_arr = None
    for slip in (1, 2):
        C.SLIP_TICKS = slip
        cell = C.evaluate(trades, ts, sym)
        if slip == 1:
            spec = C.SPECS[sym]
            cost = spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
            net_arr = np.array([(xp - ep) * side * spec["pt"] - cost
                                for _, _, ep, xp, side in trades])
        tag = f"{slip}-tick"
        print(f"  [{label} {sym} {tag}] n={cell.get('n')} "
              f"avg=${cell.get('avg_usd')} PF={cell.get('pf')} t={cell.get('t')} "
              f"p={cell.get('p_one_sided')} boot={cell.get('p_bootstrap')} "
              f"yrs+={cell.get('pct_years_positive')}%")
    SR, SR0, hair, dsr = deflated_sharpe(net_arr)
    print(f"      per-trade Sharpe={SR:.4f}  SR0(N={N_TRIALS})={SR0:.4f}  "
          f"deflated(SR-SR0)={hair:+.4f}  DSR_prob={dsr:.3f}")
    C.SLIP_TICKS = 1
    return net_arr


def main():
    print("=" * 78)
    print("  ROUND 27 — Turn-of-month intraday drift (ES) — SEARCH SET ONLY")
    print("  (holdout 2025-06-05..2026-06-05 is LOCKED and NOT evaluated here)")
    print("=" * 78)
    for sym in ("ES", "MES"):
        ts, tom = build(sym, want_tom=True)
        _, base = build(sym, want_tom=False)
        print(f"\n--- {sym} ---")
        tom_net = report(sym, "TOM", tom, ts)
        base_net = report(sym, "non-TOM baseline", base, ts)
        if tom_net is not None and base_net is not None and len(base_net):
            print(f"      TOM avg ${tom_net.mean():+.2f}/trade vs baseline "
                  f"${base_net.mean():+.2f}/trade  "
                  f"(differential ${tom_net.mean()-base_net.mean():+.2f})")
    print("\nPASS bar (ES, 1-tick, SEARCH): n>=200, PF>=1.15, p<0.05 (t AND boot),")
    print("yrs+>=60%, deflated(SR-SR0)>0. Any fail -> KILL, holdout stays locked.")


if __name__ == "__main__":
    main()
