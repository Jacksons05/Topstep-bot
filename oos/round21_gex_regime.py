"""Round 21 — GEX vol-regime toggle OOS test (the live gex_strategy.py rules).

Implements exactly the pre-registered spec in HYPOTHESES.md (Round 21, frozen
2026-07-16 in commit d701ce4 BEFORE this file ran). Reuses the Round-2 harness
kernels (load / _atr / mins / evaluate / passes) so costs, stats and the PASS
bar are computed by the same code every prior round was judged with.

One ambiguity in the registered text was resolved CONSERVATIVELY before any
results were seen: "prior 20 RTH bars" for the breakout leg means prior bars
WITHIN the same session (first signal possible ~11:10), so an overnight gap
can never manufacture a trivial "breakout" at the open. Noted here because
the live code uses the last 20 bars of a continuous feed instead.

Usage:  .venv/bin/python oos/round21_gex_regime.py
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from candidates import SPECS, _atr, evaluate, load, mins, passes  # noqa: E402

DATA = HERE / "data"

# ── Registered regime parameters (mirror uw_gex.py / config defaults) ────────
BAND_FRAC = 0.25          # GEX_NEUTRAL_BAND_FRAC
BAND_WINDOW = 250         # rolling median window (days strictly before t)
BAND_MIN_OBS = 60         # fewer prior obs -> neutral (no trade)
MR_ATR_DEV = 1.0          # GEX_MR_ATR_DEV
BREAKOUT_LOOKBACK = 20    # GEX_BREAKOUT_LOOKBACK
ATR_STOP_MULT = 2.0       # live ATR_STOP_MULT
ATR_TARGET_MULT = 3.0     # live ATR_TARGET_MULT
ENTRY_FIRST_MIN = 9 * 60 + 35    # signal-bar close window (ET)
ENTRY_LAST_MIN = 15 * 60 + 30
FLATTEN_MIN = 15 * 60 + 55       # hard flatten bar


def load_gex_regimes() -> dict[date, str]:
    """date -> regime governing THAT session (from the prior GEX close).

    GEX_t (known at close of day t) governs session t+1: for each GEX date we
    compute the regime label from GEX_t vs 0.25 x median(|GEX| of the 250
    days STRICTLY BEFORE t), then assign it to every calendar day AFTER t up
    to and including the next GEX date. Lookahead-safe by construction.
    """
    rows: list[tuple[date, float]] = []
    with (DATA / "squeeze_dix_gex.csv").open() as f:
        for r in csv.DictReader(f):
            rows.append((date.fromisoformat(r["date"]), float(r["gex"])))
    rows.sort()
    labels: list[tuple[date, str]] = []   # (gex_date, regime it emits for later days)
    hist: list[float] = []
    for d, g in rows:
        if len(hist) >= BAND_MIN_OBS:
            band = BAND_FRAC * statistics.median(abs(x) for x in hist[-BAND_WINDOW:])
            regime = "positive" if g > band else ("negative" if g < -band else "neutral")
        else:
            regime = "neutral"           # not enough history -> no trade (fail closed)
        labels.append((d, regime))
        hist.append(g)
    # session date -> regime of the most recent GEX date STRICTLY before it
    out: dict[date, str] = {}
    for (d, regime), nxt in zip(labels, [x[0] for x in labels[1:]] + [None]):
        # regime from close of d applies to every session after d, until the
        # next GEX close supersedes it (covers weekends/holidays correctly).
        d_next = nxt or date(2100, 1, 1)
        cur = d.toordinal() + 1
        while cur <= d_next.toordinal():
            out[date.fromordinal(cur)] = regime
            cur += 1
    return out


def run_symbol(sym: str):
    ts, o, h, l, c, v = load(sym)
    atr = _atr(h, l, c)
    regimes = load_gex_regimes()

    # RTH bar indices per session
    by_day: dict[date, list[int]] = {}
    for i, t in enumerate(ts):
        if t.weekday() < 5 and 9 * 60 + 30 <= mins(t) <= FLATTEN_MIN:
            by_day.setdefault(t.date(), []).append(i)

    trades_mr, trades_bo = [], []
    day_counts = {"positive": 0, "negative": 0, "neutral": 0, "unlabeled": 0}

    for day, idxs in sorted(by_day.items()):
        regime = regimes.get(day)
        if regime is None:
            day_counts["unlabeled"] += 1
            continue
        day_counts[regime] += 1
        if regime == "neutral":
            continue

        cum_pv = cum_v = 0.0
        pos = None   # (side, entry_px, stop, target, entry_i)
        for k, i in enumerate(idxs):
            m = mins(ts[i])
            cum_pv += c[i] * v[i]        # engine _session_vwap: closes x volume
            cum_v += v[i]

            if pos is not None:
                side, epx, stop, tgt, ei = pos
                exited = None
                if side > 0:
                    if l[i] <= stop:
                        exited = stop            # stop first: both-hit -> stop
                    elif h[i] >= tgt:
                        exited = tgt
                elif side < 0:
                    if h[i] >= stop:
                        exited = stop
                    elif l[i] <= tgt:
                        exited = tgt
                if exited is None and m >= FLATTEN_MIN:
                    exited = c[i]
                if exited is not None:
                    (trades_mr if regime == "positive" else trades_bo).append(
                        (ei, i, epx, exited, side))
                    pos = None
                continue

            # flat: look for an entry signal on this bar's close
            if not (ENTRY_FIRST_MIN <= m <= ENTRY_LAST_MIN) or k + 1 >= len(idxs):
                continue
            a = atr[i]
            if np.isnan(a) or a <= 0:
                continue
            side = 0
            if regime == "positive":
                if cum_v <= 0:
                    continue
                vwap = cum_pv / cum_v
                dev = (c[i] - vwap) / a
                if dev <= -MR_ATR_DEV:
                    side = 1
                elif dev >= MR_ATR_DEV:
                    side = -1
            else:  # negative -> breakout, prior 20 same-session bars only
                if k < BREAKOUT_LOOKBACK:
                    continue
                win = idxs[k - BREAKOUT_LOOKBACK:k]
                hi = max(h[j] for j in win)
                lo = min(l[j] for j in win)
                if c[i] > hi:
                    side = 1
                elif c[i] < lo:
                    side = -1
            if side:
                fi = idxs[k + 1]
                epx = c[fi]
                stop = epx - side * ATR_STOP_MULT * a
                tgt = epx + side * ATR_TARGET_MULT * a
                pos = (side, epx, stop, tgt, fi)

        if pos is not None:              # safety: flatten at last session bar
            side, epx, stop, tgt, ei = pos
            last = idxs[-1]
            (trades_mr if regime == "positive" else trades_bo).append(
                (ei, last, epx, c[last], side))

    return ts, trades_mr, trades_bo, day_counts


def main() -> int:
    results = {}
    for sym in ("ES", "MES"):
        ts, mr, bo, days = run_symbol(sym)
        pooled = sorted(mr + bo, key=lambda t: t[0])
        results[sym] = {
            "days": days,
            "primary_pooled": evaluate(pooled, ts, sym),
            "secondary_mr_positive": evaluate(mr, ts, sym),
            "secondary_breakout_negative": evaluate(bo, ts, sym),
        }
    verdict = "PASS" if passes(results["ES"]["primary_pooled"]) else "FAIL"
    out = {
        "registered": "Round 21 (HYPOTHESES.md, commit d701ce4, 2026-07-16)",
        "judged_on": "ES pooled @ 1-tick slip, net",
        "verdict_primary": verdict,
        "secondary_verdicts": {
            "mr_positive": "PASS" if passes(results["ES"]["secondary_mr_positive"]) else "FAIL",
            "breakout_negative": "PASS" if passes(results["ES"]["secondary_breakout_negative"]) else "FAIL",
        },
        "cells": results,
    }
    (HERE / "round21_results.json").write_text(json.dumps(out, indent=1))
    print(f"PRIMARY (ES pooled): {verdict}")
    for sym in ("ES", "MES"):
        print(f"\n{sym}  days={results[sym]['days']}")
        for cell in ("primary_pooled", "secondary_mr_positive", "secondary_breakout_negative"):
            r = results[sym][cell]
            print(f"  {cell:28} n={r.get('n', 0):5} total=${r.get('total_usd', 0):>12} "
                  f"PF={r.get('pf')} t={r.get('t')} p={r.get('p_one_sided')} "
                  f"boot={r.get('p_bootstrap')} yrs+={r.get('pct_years_positive')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
