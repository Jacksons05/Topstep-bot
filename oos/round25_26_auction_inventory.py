"""Round 25 (failed-auction fade) + Round 26 (overnight inventory reversal).

Implements exactly the registered specs (HYPOTHESES.md Rounds 25/26, commit
7977157, frozen BEFORE this file ran). Reuses the Round-2 harness kernels
(load / _atr / mins / evaluate / passes) and the Round-24 correct-fill
discipline (enter next-bar OPEN, valid-geometry-only, both-hit=stop, no
entry-bar exit).

Usage:  .venv/bin/python oos/round25_26_auction_inventory.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from candidates import evaluate, _atr, load, mins, passes  # noqa: E402

RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 16 * 60
ENTRY_FIRST, ENTRY_LAST = 9 * 60 + 35, 15 * 60 + 30
FLATTEN = 15 * 60 + 55


def _rth_days(ts):
    by_day = defaultdict(list)
    for i, t in enumerate(ts):
        if t.weekday() < 5 and RTH_OPEN <= mins(t) <= FLATTEN:
            by_day[t.date()].append(i)
    return by_day


def _simulate_exit(ts, idxs, start_k, side, epx, stop, tgt, o, h, l, c):
    """Walk bars from start_k+1; both-hit=stop; else 15:55 flatten. Returns
    (exit_idx, exit_px)."""
    for j in range(start_k + 1, len(idxs)):
        i = idxs[j]
        if side > 0:
            if l[i] <= stop:
                return i, stop
            if h[i] >= tgt:
                return i, tgt
        else:
            if h[i] >= stop:
                return i, stop
            if l[i] <= tgt:
                return i, tgt
        if mins(ts[i]) >= FLATTEN:
            return i, c[i]
    last = idxs[-1]
    return last, c[last]


# ── Round 25: failed-auction fade ────────────────────────────────────────────

def run_failed_auction(sym):
    ts, o, h, l, c, v = load(sym)
    atr = _atr(h, l, c)
    by_day = _rth_days(ts)
    days = sorted(by_day)
    # prior-day H/L/MID from each RTH session
    ext = {}
    for d in days:
        idxs = by_day[d]
        ext[d] = (max(h[i] for i in idxs), min(l[i] for i in idxs))
    prev = {d: days[i - 1] for i, d in enumerate(days) if i > 0}

    trades, stats = [], {"days": 0, "signals": 0}
    for d in days:
        pd = prev.get(d)
        if pd is None:
            continue
        pdh, pdl = ext[pd]
        pdmid = (pdh + pdl) / 2.0
        idxs = by_day[d]
        stats["days"] += 1
        sess_hi, sess_lo = -1e18, 1e18
        broke_hi = broke_lo = False
        done_short = done_long = False
        pos = None
        for k, i in enumerate(idxs):
            sess_hi = max(sess_hi, h[i])
            sess_lo = min(sess_lo, l[i])
            if h[i] > pdh:
                broke_hi = True
            if l[i] < pdl:
                broke_lo = True
            if pos is not None:
                continue  # one position at a time (handled via exit sim below)
            m = mins(ts[i])
            if not (ENTRY_FIRST <= m <= ENTRY_LAST) or k + 1 >= len(idxs):
                continue
            a = atr[i]
            if np.isnan(a) or a <= 0:
                continue
            side = 0
            if broke_hi and not done_short and c[i] < pdh:
                side, stop = -1, sess_hi + 0.25 * a
                done_short = True
            elif broke_lo and not done_long and c[i] > pdl:
                side, stop = 1, sess_lo - 0.25 * a
                done_long = True
            if side == 0:
                continue
            ei = idxs[k + 1]
            epx = o[ei]
            tgt = pdmid
            if side < 0 and not (tgt < epx < stop):
                continue
            if side > 0 and not (stop < epx < tgt):
                continue
            stats["signals"] += 1
            ex_i, ex_px = _simulate_exit(ts, idxs, k + 1, side, epx, stop, tgt, o, h, l, c)
            trades.append((ei, ex_i, epx, ex_px, side))
            pos = "done"  # only re-scan after; simple: one trade/day per direction
            pos = None    # allow the other direction later same day
    return ts, trades, stats


# ── Round 26: overnight inventory reversal ───────────────────────────────────

def run_overnight_inventory(sym):
    ts, o, h, l, c, v = load(sym)
    atr = _atr(h, l, c)
    by_day = _rth_days(ts)
    days = sorted(by_day)
    # prior RTH close (16:00 side) = last RTH bar's close; RTH open = first bar open
    rth_close = {d: c[by_day[d][-1]] for d in days}
    rth_open = {d: o[by_day[d][0]] for d in days}
    prev = {d: days[i - 1] for i, d in enumerate(days) if i > 0}

    # overnight move series + trailing-60 tercile threshold (causal)
    on_hist = []
    trades, stats = [], {"days": 0, "signals": 0}
    for d in days:
        pd = prev.get(d)
        if pd is None:
            continue
        on = rth_open[d] - rth_close[pd]
        stats["days"] += 1
        if len(on_hist) >= 60:
            thr = np.percentile([abs(x) for x in on_hist[-60:]], 66.6667)
        else:
            thr = None
        on_hist.append(on)
        if thr is None or abs(on) < thr or abs(on) <= 0:
            continue
        idxs = by_day[d]
        a = atr[idxs[0]]
        if np.isnan(a) or a <= 0:
            continue
        side = -1 if on > 0 else 1          # fade the overnight move
        epx = rth_open[d]                    # enter at the 09:30 open
        tgt = rth_close[pd]                  # revert to pre-overnight level
        stop = epx + (1.0 * a if side < 0 else -1.0 * a)
        if side < 0 and not (tgt < epx < stop):
            continue
        if side > 0 and not (stop < epx < tgt):
            continue
        stats["signals"] += 1
        ex_i, ex_px = _simulate_exit(ts, idxs, 0, side, epx, stop, tgt, o, h, l, c)
        trades.append((idxs[0], ex_i, epx, ex_px, side))
    return ts, trades, stats


def main() -> int:
    
    out = {"registered": "Rounds 25/26 (7977157)"}
    for label, fn, key in (("Round 25 failed-auction", run_failed_auction, "round25"),
                           ("Round 26 overnight-inventory", run_overnight_inventory, "round26")):
        cells = {}
        for sym in ("ES", "MES"):
            ts, trades, stats = fn(sym)
            cell = evaluate(trades, ts, sym)
            cell["funnel"] = stats
            cells[sym] = cell
        verdict = "PASS" if passes(cells["ES"]) else "FAIL"
        out[key] = {"verdict": verdict, "cells": cells}
        print(f"\n=== {label}: ES {verdict} ===")
        for sym in ("ES", "MES"):
            r = cells[sym]
            print(f"  {sym} funnel={r['funnel']} | n={r.get('n', 0)} "
                  f"total=${r.get('total_usd', 0)} PF={r.get('pf')} t={r.get('t')} "
                  f"p={r.get('p_one_sided')} yrs+={r.get('pct_years_positive')}% win={r.get('win_pct')}%")
    (HERE / "round25_26_results.json").write_text(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
