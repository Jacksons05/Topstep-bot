"""LOCKED-HOLDOUT confirmation (ONE-SHOT) for the NQ overnight drift — the single
best candidate of the 31-round program. Burns the holdout 2025-06-05..2026-06-05.

Same frozen R30 methodology (LONG 18:00 ET session open -> 09:30 ET next RTH open),
but evaluated ONLY on the locked holdout window that was never touched during search.
Reports search vs holdout side by side. NOT a Topstep-viability test (the overnight-
gap tail is DLL/MLL-fatal regardless) — this answers "is the NQ overnight edge real
out-of-sample, i.e. worth a fork-B design in a venue that allows overnight holds?"

Usage: .venv/bin/python oos/round30_nq_holdout.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402
from round30_overnight_drift import deflated_sharpe, netarr, EVE_MIN, RTH_OPEN_MIN  # noqa: E402

SEARCH_END = date(2025, 6, 5)     # holdout starts here
HOLD_END = date(2026, 6, 6)       # data ends ~2026-06-05


def build(sym, lo, hi):
    """Overnight LONG trades whose RTH-morning exit date D is in [lo, hi)."""
    ts, o, h, l, c, v = C.load(sym)
    by_date = defaultdict(list)
    for i, t in enumerate(ts):
        by_date[t.date()].append(i)
    trades = []
    for E in sorted(by_date):
        if E.weekday() not in (6, 0, 1, 2, 3):
            continue
        D = E + timedelta(days=1)
        if D not in by_date or not (lo <= D < hi):
            continue
        entry_i = next((i for i in by_date[E] if C.mins(ts[i]) >= EVE_MIN), None)
        exit_i = next((i for i in by_date[D] if C.mins(ts[i]) >= RTH_OPEN_MIN), None)
        if entry_i is None or exit_i is None:
            continue
        trades.append((entry_i, exit_i, o[entry_i], o[exit_i], +1))
    return ts, trades


def line(sym, tag, ts, trades):
    for slip in (1, 2):
        C.SLIP_TICKS = slip
        cell = C.evaluate(trades, ts, sym)
        print(f"  [{tag:<8} {slip}t] n={cell.get('n')} avg=${cell.get('avg_usd')} "
              f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
              f"boot={cell.get('p_bootstrap')} yrs+={cell.get('pct_years_positive')}%")
    C.SLIP_TICKS = 1
    net = netarr(sym, trades, 1)
    SR, SR0, hair, dsr = deflated_sharpe(net)
    print(f"       Sharpe={SR:.4f}  worst=${net.min():,.0f}  P(loss>$500)={100*(net<-500).mean():.1f}%")


def main():
    print("=" * 78)
    print("  NQ OVERNIGHT DRIFT — LOCKED HOLDOUT confirmation (ONE-SHOT, burns holdout)")
    print("  MNQ; LONG 18:00 ET -> 09:30 ET. search vs holdout 2025-06-05..2026-06-05")
    print("=" * 78)
    for sym in ("MNQ", "ES"):
        print(f"\n--- {sym} ---")
        ts, s = build(sym, date(2010, 1, 1), SEARCH_END)
        _, hh = build(sym, SEARCH_END, HOLD_END)
        line(sym, "SEARCH", ts, s)
        line(sym, "HOLDOUT", ts, hh)


if __name__ == "__main__":
    main()
