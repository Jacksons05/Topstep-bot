"""Screen (B): Initial-Balance (IB) break — continuation or failure?

Mechanism. The IB = high-low of the first RTH hour (09:30-10:30 ET) frames the
day's initial value. Two opposite, testable regularities:
  * CONTINUATION (trend day): a break beyond IBH/IBL is accepted and extends ->
    trade WITH the break.
  * FAILURE (rotational day): the break fails and price rotates back toward IB
    -> FADE the break.
One instrument (ES), OHLC-computable at our resolution (NOT microstructure).

CHARTER CAVEAT: this is adjacent to dead rounds — R2 (ORB) and R24 (value-area
rotation) and R25 (failed-auction fade) all FAILED. High prior it re-confirms.
So we test the MECHANISM cheaply first, parameter-light: after the first IB
break post-10:30, enter next-bar open and HOLD TO 15:55. Continuation PnL is
(exit-entry)*break_side; FADE PnL is the mirror. If neither beats one-leg
friction held-to-close, there's nothing to tune and it's dead. Same evaluate
kernel / cost model as R2/R24/R26.  Judged on ES (MES reported).
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

RTH_OPEN, IB_END, FLATTEN = 9 * 60 + 30, 10 * 60 + 30, 15 * 60 + 55


def _rth_days(ts):
    by_day = defaultdict(list)
    for i, t in enumerate(ts):
        if t.weekday() < 5 and RTH_OPEN <= C.mins(t) <= FLATTEN:
            by_day[t.date()].append(i)
    return by_day


def build_breaks(sym):
    """First IB break per day -> a (entry_idx, exit_idx, entry_px, exit_px,
    break_side) tuple with exit = 15:55 flatten close. break_side=+1 up, -1 down."""
    ts, o, h, l, c, v = C.load(sym)
    by_day = _rth_days(ts)
    cont = []            # continuation trades (side = break dir)
    stats = {"days": 0, "with_full_ib": 0, "breaks": 0, "up": 0, "down": 0}
    reach_end_in_dir = 0  # base rate: close_1555 beyond entry in break dir
    for d, idxs in sorted(by_day.items()):
        stats["days"] += 1
        ib = [i for i in idxs if C.mins(ts[i]) < IB_END]     # 09:30-10:30 bars
        post = [i for i in idxs if C.mins(ts[i]) >= IB_END]  # 10:30+ bars
        if len(ib) < 12 or len(post) < 2:
            continue
        stats["with_full_ib"] += 1
        ibh = max(h[i] for i in ib)
        ibl = min(l[i] for i in ib)
        if ibh - ibl <= 0:
            continue
        # first post-10:30 bar that CLOSES beyond the IB, with room to enter next bar
        sig_k = None
        side = 0
        for k in range(len(post) - 1):
            i = post[k]
            if c[i] > ibh:
                sig_k, side = k, 1
                break
            if c[i] < ibl:
                sig_k, side = k, -1
                break
        if sig_k is None:
            continue
        stats["breaks"] += 1
        stats["up" if side > 0 else "down"] += 1
        ei = post[sig_k + 1]
        epx = o[ei]                      # honest next-open fill
        ex_i = idxs[-1]                  # hold to 15:55 flatten
        ex_px = c[ex_i]
        if (ex_px - epx) * side > 0:
            reach_end_in_dir += 1
        cont.append((ei, ex_i, epx, ex_px, side))
    stats["cont_base_rate"] = round(reach_end_in_dir / stats["breaks"], 3) if stats["breaks"] else None
    return ts, cont, stats


def mirror(trades):
    """FADE = flip the side on the same entry/exit."""
    return [(ei, xi, ep, xp, -side) for (ei, xi, ep, xp, side) in trades]


def decade_split(cell):
    y = cell.get("yearly_usd", {})
    return (round(sum(v for k, v in y.items() if int(k) < 2020), 0),
            round(sum(v for k, v in y.items() if int(k) >= 2020), 0))


def report(sym):
    print(f"\n{'='*78}\n  {sym}  — Initial-Balance break, hold-to-close event study\n{'='*78}")
    ts, cont, stats = build_breaks(sym)
    print(f"  days={stats['days']}  full-IB days={stats['with_full_ib']}  "
          f"first-breaks={stats['breaks']} (up {stats['up']} / down {stats['down']})")
    print(f"  continuation base rate (close_1555 beyond entry in break dir): "
          f"{stats['cont_base_rate']}")
    print(f"\n  {'direction':<14}{'slip':>5}{'n':>6}{'PF':>7}{'t':>7}{'p':>9}"
          f"{'yrs+':>7}{'total$':>11}{'pre2020':>10}{'post2020':>11}")
    for name, trades in (("CONTINUATION", cont), ("FADE", mirror(cont))):
        for slip in (1, 2):
            C.SLIP_TICKS = slip
            cell = C.evaluate(trades, ts, sym)
            if cell.get("n", 0) == 0:
                continue
            pre, post = decade_split(cell)
            print(f"  {name:<14}{slip:>5}{cell['n']:>6}{cell.get('pf') or 0:>7.3f}"
                  f"{cell.get('t') or 0:>7.2f}{cell.get('p_one_sided') or 1:>9.3f}"
                  f"{cell.get('pct_years_positive', 0):>6.0f}%{cell['total_usd']:>11,.0f}"
                  f"{pre:>10,.0f}{post:>11,.0f}")
    C.SLIP_TICKS = 1


def main():
    for sym in ("ES", "MES"):
        report(sym)
    print("\nHold-to-close, one leg. If neither CONTINUATION nor FADE clears costs")
    print("held to the close, there is no base edge to bracket-tune. PASS bar:")
    print("n>=200, PF>=1.15, p<0.05, yrs+>=60% (ES).")


if __name__ == "__main__":
    main()
