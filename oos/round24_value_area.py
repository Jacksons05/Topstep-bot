"""Round 24 — Market/Volume-Profile value-area rotation.

Implements exactly the registered spec (HYPOTHESES.md Round 24, commit da4dfb4,
frozen BEFORE this file ran). Reuses the Round-2 harness kernels (load / _atr /
mins / evaluate / passes) so costs, statistics and the PASS bar are computed by
the same code every prior round was judged with.

Usage:  .venv/bin/python oos/round24_value_area.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from candidates import SPECS, _atr, evaluate, load, mins, passes  # noqa: E402

VA_PCT = 0.70          # value area = 70% of session volume
BIN = 1.0              # ES 1-point volume bins
RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 16 * 60
ENTRY_FIRST, ENTRY_LAST = 9 * 60 + 35, 15 * 60 + 30
FLATTEN = 15 * 60 + 55
ATR_STOP = 1.0


def session_value_area(closes, vols):
    """POC / VAH / VAL from a session's (close, volume) via 1-pt bins.
    Expand from POC adding the larger-volume adjacent bin until >=70% enclosed."""
    hist = defaultdict(float)
    for c, v in zip(closes, vols):
        hist[round(c / BIN) * BIN] += v
    if not hist:
        return None
    prices = sorted(hist)
    total = sum(hist.values())
    if total <= 0:
        return None
    poc = max(prices, key=lambda p: hist[p])
    lo = hi = prices.index(poc)
    enclosed = hist[poc]
    while enclosed < VA_PCT * total and (lo > 0 or hi < len(prices) - 1):
        below = hist[prices[lo - 1]] if lo > 0 else -1.0
        above = hist[prices[hi + 1]] if hi < len(prices) - 1 else -1.0
        if above >= below:
            hi += 1
            enclosed += hist[prices[hi]]
        else:
            lo -= 1
            enclosed += hist[prices[lo]]
    return poc, prices[hi], prices[lo]   # POC, VAH, VAL


def build_daily_va(ts, c, v):
    """Per RTH session (date -> (POC, VAH, VAL)), settled at the close."""
    by_day = defaultdict(lambda: ([], []))
    for i, t in enumerate(ts):
        if t.weekday() < 5 and RTH_OPEN <= mins(t) < RTH_CLOSE:
            by_day[t.date()][0].append(c[i])
            by_day[t.date()][1].append(v[i])
    va = {}
    for day, (cc, vv) in by_day.items():
        r = session_value_area(cc, vv)
        if r is not None:
            va[day] = r
    return va


def run_symbol(sym):
    ts, o, h, l, c, v = load(sym)
    atr = _atr(h, l, c)
    va = build_daily_va(ts, c, v)
    days_sorted = sorted(va)
    prev_of = {d: days_sorted[i - 1] for i, d in enumerate(days_sorted) if i > 0}

    # RTH bar indices per session
    by_day = defaultdict(list)
    for i, t in enumerate(ts):
        if t.weekday() < 5 and RTH_OPEN <= mins(t) <= FLATTEN:
            by_day[t.date()].append(i)

    trades = []
    stats = {"days": 0, "balanced_open": 0, "signals": 0}
    for day in sorted(by_day):
        pdv = prev_of.get(day)
        if pdv is None or pdv not in va:
            continue
        poc, vah, val = va[pdv]
        idxs = by_day[day]
        if not idxs:
            continue
        stats["days"] += 1
        # balanced-open gate: today's 09:30 open inside prior value area
        open_px = o[idxs[0]]
        if not (val <= open_px <= vah):
            continue
        stats["balanced_open"] += 1

        pos = None  # (side, entry_px, stop, target, entry_i)
        for k, i in enumerate(idxs):
            m = mins(ts[i])
            if pos is not None:
                side, epx, stop, tgt, ei = pos
                if i <= ei:
                    continue                    # never exit on the entry bar itself
                                                # (we entered at its close; its
                                                # intrabar range predates entry)
                exited = None
                if side > 0:
                    if l[i] <= stop:
                        exited = stop
                    elif h[i] >= tgt:
                        exited = tgt
                else:
                    if h[i] >= stop:
                        exited = stop
                    elif l[i] <= tgt:
                        exited = tgt
                if exited is None and m >= FLATTEN:
                    exited = c[i]
                if exited is not None:
                    trades.append((ei, i, epx, exited, side))
                    pos = None
                continue
            if not (ENTRY_FIRST <= m <= ENTRY_LAST) or k + 1 >= len(idxs):
                continue
            a = atr[i]
            if np.isnan(a) or a <= 0:
                continue
            side = 0
            if c[i] >= vah:
                side = -1                       # above prior value → fade short to POC
            elif c[i] <= val:
                side = 1                        # below prior value → fade long to POC
            if side == 0:
                continue
            ei = idxs[k + 1]
            epx = o[ei]                          # enter NEXT bar's OPEN (realistic;
                                                 # decision made on bar i's close)
            tgt = poc
            stop = (vah + ATR_STOP * a) if side < 0 else (val - ATR_STOP * a)
            # Valid geometry only: entry must sit strictly BETWEEN target and stop
            # in the trade's favour. A next-open that has already blown past the
            # POC target, or already past the stop, is not a tradeable rotation —
            # skip it rather than book a degenerate fill (the PF-15 artifact).
            if side < 0 and not (tgt < epx < stop):
                continue
            if side > 0 and not (stop < epx < tgt):
                continue
            stats["signals"] += 1
            pos = (side, epx, stop, tgt, ei)
        if pos is not None:
            side, epx, stop, tgt, ei = pos
            last = idxs[-1]
            trades.append((ei, last, epx, c[last], side))
    return ts, trades, stats


def main() -> int:
    results = {}
    for sym in ("ES", "MES"):
        ts, trades, stats = run_symbol(sym)
        cell = evaluate(trades, ts, sym)
        cell["funnel"] = stats
        results[sym] = cell
    verdict = "PASS" if passes(results["ES"]) else "FAIL"
    out = {"registered": "Round 24 (da4dfb4)", "judged_on": "ES @ 1-tick slip, net",
           "verdict": verdict, "cells": results}
    (HERE / "round24_results.json").write_text(json.dumps(out, indent=1))
    print(f"ROUND 24 VERDICT (ES): {verdict}\n")
    for sym in ("ES", "MES"):
        r = results[sym]
        print(f"{sym}  funnel={r['funnel']}")
        print(f"   n={r.get('n', 0)} total=${r.get('total_usd', 0)} PF={r.get('pf')} "
              f"t={r.get('t')} p={r.get('p_one_sided')} boot={r.get('p_bootstrap')} "
              f"yrs+={r.get('pct_years_positive')}% win={r.get('win_pct')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
