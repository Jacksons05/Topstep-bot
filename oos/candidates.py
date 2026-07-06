"""Round-2 candidate strategies on the Databento OOS data.

Implements exactly the pre-registered specs in HYPOTHESES.md (Round 2):
C1 overnight drift, C2 opening-range breakout, C3 VWAP reversion.
No parameters beyond those written there. Judged at 1-tick slippage, net.

Usage:  .venv/bin/python oos/candidates.py
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
SPECS = {
    "ES":  {"pt": 50.0, "tick": 0.25, "comm_rt": 4.00},
    "MES": {"pt": 5.0,  "tick": 0.25, "comm_rt": 1.40},
    "MNQ": {"pt": 2.0,  "tick": 0.25, "comm_rt": 1.40},
}
SLIP_TICKS = 1
BOOT_N = 20_000
RNG_SEED = 7


def load(sym):
    ts, o, h, l, c, v = [], [], [], [], [], []
    with (DATA / f"{sym}_5min.csv").open() as f:
        for row in csv.DictReader(f):
            dt = datetime.fromisoformat(row["timestamp"]).astimezone(ET)
            ts.append(dt)
            o.append(float(row["open"]))
            h.append(float(row["high"]))
            l.append(float(row["low"]))
            c.append(float(row["close"]))
            v.append(float(row["volume"]))
    return ts, np.array(o), np.array(h), np.array(l), np.array(c), np.array(v)


def _atr(h, l, c, period=14):
    n = len(c)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    pc = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    cs = np.cumsum(np.insert(tr, 0, 0.0))
    out[period - 1:] = (cs[period:] - cs[:-period]) / period
    return out


def mins(dt):
    return dt.hour * 60 + dt.minute


def c1_overnight_drift(ts, o, h, l, c, v):
    """Buy at 16:00 bar close, exit next session first bar close >= 09:30."""
    trades = []
    entry_i = None
    for i, t in enumerate(ts):
        if t.weekday() >= 5:
            continue
        m = mins(t)
        if entry_i is None:
            if m == 16 * 60:
                entry_i = i
        else:
            # next calendar day (or later), first bar closing at/after 09:30 RTH
            if t.date() > ts[entry_i].date() and 9 * 60 + 30 <= m < 16 * 60:
                trades.append((entry_i, i, c[entry_i], c[i], 1))
                entry_i = None
    return trades


def c2_orb(ts, o, h, l, c, v):
    """09:30-09:55 range; 10:00-12:00 first close beyond range; stop far side; flat 15:55."""
    trades = []
    by_day = {}
    for i, t in enumerate(ts):
        if t.weekday() < 5:
            by_day.setdefault(t.date(), []).append(i)
    for day, idxs in by_day.items():
        rng = [i for i in idxs if 9 * 60 + 30 <= mins(ts[i]) <= 9 * 60 + 55]
        if len(rng) < 6:
            continue
        hi, lo = h[rng].max(), l[rng].min()
        window = [i for i in idxs if 10 * 60 <= mins(ts[i]) <= 15 * 60 + 55]
        pos = None  # (side, entry_px, stop)
        for k, i in enumerate(window):
            m = mins(ts[i])
            if pos is None:
                if m >= 12 * 60:
                    break
                if k + 1 >= len(window):
                    break
                if c[i] > hi:
                    pos = (1, c[window[k + 1]], lo, window[k + 1])
                elif c[i] < lo:
                    pos = (-1, c[window[k + 1]], hi, window[k + 1])
            else:
                side, epx, stop, ei = pos
                if side > 0 and l[i] <= stop:
                    trades.append((ei, i, epx, stop, side)); pos = None; break
                if side < 0 and h[i] >= stop:
                    trades.append((ei, i, epx, stop, side)); pos = None; break
                if m >= 15 * 60 + 55:
                    trades.append((ei, i, epx, c[i], side)); pos = None; break
        if pos is not None:
            side, epx, stop, ei = pos
            last = window[-1]
            trades.append((ei, last, epx, c[last], side))
    return trades


def c3_vwap_reversion(ts, o, h, l, c, v):
    """Fade >2-sigma deviation from session VWAP; exit VWAP cross / 2xATR stop / 15:55."""
    trades = []
    atr = _atr(h, l, c)
    tp = (h + l + c) / 3.0
    by_day = {}
    for i, t in enumerate(ts):
        if t.weekday() < 5 and 9 * 60 + 30 <= mins(ts[i]) <= 15 * 60 + 55:
            by_day.setdefault(t.date(), []).append(i)
    for day, idxs in by_day.items():
        cum_pv = 0.0
        cum_v = 0.0
        devs = []
        pos = None
        for k, i in enumerate(idxs):
            cum_pv += tp[i] * v[i]
            cum_v += v[i]
            if cum_v <= 0:
                continue
            vwap = cum_pv / cum_v
            dev = c[i] - vwap
            devs.append(dev)
            m = mins(ts[i])
            if pos is None:
                if not (10 * 60 <= m <= 15 * 60 + 30) or len(devs) < 20 or k + 1 >= len(idxs):
                    continue
                sigma = float(np.std(devs[-20:], ddof=1))
                if sigma <= 0 or np.isnan(atr[i]):
                    continue
                if dev > 2 * sigma:
                    pos = (-1, c[idxs[k + 1]], c[idxs[k + 1]] + 2 * atr[i], idxs[k + 1])
                elif dev < -2 * sigma:
                    pos = (1, c[idxs[k + 1]], c[idxs[k + 1]] - 2 * atr[i], idxs[k + 1])
            else:
                side, epx, stop, ei = pos
                if side > 0 and (h[i] >= vwap or l[i] <= stop or m >= 15 * 60 + 55):
                    xpx = stop if l[i] <= stop else (vwap if h[i] >= vwap else c[i])
                    trades.append((ei, i, epx, xpx, side)); pos = None
                elif side < 0 and (l[i] <= vwap or h[i] >= stop or m >= 15 * 60 + 55):
                    xpx = stop if h[i] >= stop else (vwap if l[i] <= vwap else c[i])
                    trades.append((ei, i, epx, xpx, side)); pos = None
        if pos is not None:
            side, epx, stop, ei = pos
            last = idxs[-1]
            trades.append((ei, last, epx, c[last], side))
    return trades


def evaluate(trades, ts, sym):
    spec = SPECS[sym]
    cost = spec["comm_rt"] + 2 * SLIP_TICKS * spec["tick"] * spec["pt"]
    net = np.array([(xp - ep) * side * spec["pt"] - cost for _, _, ep, xp, side in trades])
    if len(net) == 0:
        return {"n": 0}
    t = None
    p = None
    if len(net) > 2 and net.std(ddof=1) > 0:
        from math import erf, sqrt
        t = float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))))
        p = 1 - 0.5 * (1 + erf(t / sqrt(2)))
    rng = np.random.default_rng(RNG_SEED)
    bp = float((rng.choice(net, size=(BOOT_N, len(net)), replace=True).mean(axis=1) <= 0).mean())
    gp, gl = net[net > 0].sum(), net[net <= 0].sum()
    yearly = {}
    for (ei, _, _, _, _), pnl in zip(trades, net):
        yearly[ts[ei].year] = yearly.get(ts[ei].year, 0.0) + pnl
    pos_years = sum(1 for x in yearly.values() if x > 0)
    return {
        "n": int(len(net)), "win_pct": round(100 * float((net > 0).mean()), 1),
        "total_usd": round(float(net.sum()), 2), "avg_usd": round(float(net.mean()), 2),
        "pf": round(float(gp / -gl), 3) if gl < 0 else None,
        "t": round(t, 3) if t is not None else None,
        "p_one_sided": round(p, 5) if p is not None else None,
        "p_bootstrap": round(bp, 5),
        "yearly_usd": {str(y): round(x, 2) for y, x in sorted(yearly.items())},
        "pct_years_positive": round(100 * pos_years / len(yearly), 1),
    }


def passes(cell):
    return bool(cell.get("n", 0) >= 200 and (cell.get("pf") or 0) >= 1.15
                and cell.get("p_one_sided") is not None and cell["p_one_sided"] < 0.05
                and cell.get("p_bootstrap") is not None and cell["p_bootstrap"] < 0.05
                and cell.get("pct_years_positive", 0) >= 60)


def main():
    strategies = {"C1_overnight_drift": c1_overnight_drift,
                  "C2_orb": c2_orb,
                  "C3_vwap_reversion": c3_vwap_reversion}
    results = {}
    for sym in SPECS:
        data = load(sym)
        ts = data[0]
        for name, fn in strategies.items():
            trades = fn(*data)
            cell = evaluate(trades, ts, sym)
            results.setdefault(name, {})[sym] = cell
    verdicts = {name: ("PASS" if passes(cells.get("ES", {})) else "FAIL")
                for name, cells in results.items()}
    out = {"judged_on": "ES @ 1-tick slip", "verdicts": verdicts, "cells": results}
    (OUT / "candidates_results.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(verdicts, indent=1))
    for name, cells in results.items():
        for sym, cell in cells.items():
            print(f"{name:22} {sym:4} n={cell.get('n', 0):6} total=${cell.get('total_usd', 0):>12} "
                  f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
                  f"yrs+={cell.get('pct_years_positive')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
