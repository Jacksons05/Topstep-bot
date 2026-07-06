"""Round-5 test: the bot's order-flow gate as an ENTRY DRIVER.

Spec frozen in HYPOTHESES.md Round 5 before the book data was pulled:
OBI10 z-score (trailing 30 min) >= +/-1.5 with CVD5 direction agreement ->
enter crossing the spread, exit exactly 5 minutes later crossing the spread.
$4 RT commission; crossed spread IS the slippage (1-tick sensitivity reported).

Usage:  .venv/bin/python oos/orderflow_test.py
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
PT_VALUE = 50.0
TICK = 0.25
COMM_RT = 4.00
Z_WIN = 1800      # 30 min of 1s samples
CVD_WIN = 300     # 5 min
Z_THRESH = 1.5
HOLD_SEC = 300
BOOT_N = 20_000
RNG_SEED = 7


def rolling_mean_std(x, win):
    """Trailing mean/std over win samples (NaN until warm)."""
    c1 = np.cumsum(np.insert(x, 0, 0.0))
    c2 = np.cumsum(np.insert(x * x, 0, 0.0))
    n = len(x)
    mean = np.full(n, np.nan)
    std = np.full(n, np.nan)
    m = (c1[win:] - c1[:-win]) / win
    v = (c2[win:] - c2[:-win]) / win - m * m
    mean[win - 1:] = m
    std[win - 1:] = np.sqrt(np.maximum(v, 0.0))
    return mean, std


def trailing_sum(x, win):
    c = np.cumsum(np.insert(x, 0, 0.0))
    out = np.full(len(x), np.nan)
    out[win - 1:] = c[win:] - c[:-win]
    return out


def evaluate(net, label):
    net = np.asarray(net)
    if len(net) == 0:
        return {"cell": label, "n": 0}
    t = p = None
    if len(net) > 2 and net.std(ddof=1) > 0:
        from math import erf, sqrt
        t = float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))))
        p = 1 - 0.5 * (1 + erf(t / sqrt(2)))
    rng = np.random.default_rng(RNG_SEED)
    bp = float((rng.choice(net, size=(BOOT_N, len(net)), replace=True).mean(axis=1) <= 0).mean())
    gp, gl = net[net > 0].sum(), net[net <= 0].sum()
    return {"cell": label, "n": int(len(net)),
            "win_pct": round(100 * float((net > 0).mean()), 1),
            "total_usd": round(float(net.sum()), 2),
            "avg_usd": round(float(net.mean()), 3),
            "pf": round(float(gp / -gl), 3) if gl < 0 else None,
            "t": round(t, 3) if t is not None else None,
            "p_one_sided": round(p, 5) if p is not None else None,
            "p_bootstrap": round(bp, 5)}


def main() -> int:
    d = np.load(HERE / "data" / "ES_of_1s.npz")
    sec, obi, bid, ask, tvol = d["sec"], d["obi"], d["bid"], d["ask"], d["tvol"]
    start_ns = int(d["start_ns"])
    base = datetime.fromtimestamp(start_ns / 1e9, tz=timezone.utc)

    # index by absolute second for O(1) exit lookup
    max_sec = int(sec[-1]) + 1
    pos_of = np.full(max_sec, -1, dtype=np.int64)
    pos_of[sec] = np.arange(len(sec))

    mean, std = rolling_mean_std(obi, Z_WIN)
    z = np.where(std > 1e-9, (obi - mean) / std, 0.0)
    cvd = trailing_sum(tvol, CVD_WIN)

    long_sig = (z >= Z_THRESH) & (cvd > 0)
    short_sig = (z <= -Z_THRESH) & (cvd < 0)

    trades = []
    i = 0
    n = len(sec)
    while i < n - 1:
        if np.isnan(z[i]) or np.isnan(cvd[i]) or not (long_sig[i] or short_sig[i]):
            i += 1
            continue
        entry_j = i + 1                       # next snapshot
        if sec[entry_j] - sec[i] > 5:         # feed gap — skip
            i += 1
            continue
        exit_sec = int(sec[entry_j]) + HOLD_SEC
        exit_j = -1
        for probe in range(exit_sec, min(exit_sec + 60, max_sec)):
            k = pos_of[probe]
            if k >= 0:
                exit_j = int(k)
                break
        if exit_j < 0:
            i += 1
            continue
        side = 1 if long_sig[i] else -1
        e_px = ask[entry_j] if side > 0 else bid[entry_j]     # cross the spread
        x_px = bid[exit_j] if side > 0 else ask[exit_j]
        if np.isnan(e_px) or np.isnan(x_px):
            i = exit_j + 1
            continue
        dt = base + timedelta(seconds=int(sec[entry_j]))
        loc = dt.astimezone(ET)
        rth = loc.weekday() < 5 and (9 * 60 + 30) <= loc.hour * 60 + loc.minute < 16 * 60
        trades.append({"side": side, "net": (x_px - e_px) * side * PT_VALUE - COMM_RT,
                       "net_slip1": (x_px - e_px) * side * PT_VALUE - COMM_RT - 2 * TICK * PT_VALUE,
                       "session": "RTH" if rth else "overnight",
                       "ts": dt.isoformat()})
        i = exit_j + 1                        # one position at a time

    cells = [evaluate([t["net"] for t in trades], "ALL_judged"),
             evaluate([t["net_slip1"] for t in trades], "ALL_slip1_sensitivity"),
             evaluate([t["net"] for t in trades if t["session"] == "RTH"], "RTH"),
             evaluate([t["net"] for t in trades if t["session"] == "overnight"], "overnight"),
             evaluate([t["net"] for t in trades if t["side"] > 0], "LONG_only"),
             evaluate([t["net"] for t in trades if t["side"] < 0], "SHORT_only")]
    j = cells[0]
    ok = bool(j.get("n", 0) >= 1000 and (j.get("pf") or 0) >= 1.10
              and j.get("p_one_sided") is not None and j["p_one_sided"] < 0.05
              and j.get("p_bootstrap", 1) < 0.05)
    out = {"verdict": "PASS" if ok else "FAIL", "cells": cells}
    (HERE / "orderflow_results.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
