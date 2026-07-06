"""Out-of-sample test of the bot's quant strategy on Databento history.

Runs the bot's own kernel (backtest_fast._quant_arrays + _simulate, live
config) over oos/data/{SYM}_5min.csv and judges the pre-registered hypotheses
in oos/HYPOTHESES.md. Nothing here was tuned after seeing the data.

Usage:  .venv/bin/python oos/backtest_oos.py
"""
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).resolve().parent / "data"
sys.path.insert(0, str(ROOT))

from backtest_fast import _quant_arrays, _simulate  # noqa: E402
from config import CONFIG  # noqa: E402

ET = ZoneInfo("America/New_York")
SPECS = {
    "ES":  {"pt": 50.0, "tick": 0.25, "comm_rt": 4.00},
    "MES": {"pt": 5.0,  "tick": 0.25, "comm_rt": 1.40},
    "MNQ": {"pt": 2.0,  "tick": 0.25, "comm_rt": 1.40},
}
MAX_HOLD = 24  # bars = 2h, same as in-sample trial
JUDGE_SLIP_TICKS = 1
BOOT_N = 20_000
RNG_SEED = 7


def is_rth(dt: datetime) -> bool:
    loc = dt.astimezone(ET)
    if loc.weekday() >= 5:
        return False
    mins = loc.hour * 60 + loc.minute
    return 9 * 60 + 30 <= mins < 16 * 60


def load(sym: str):
    ts, o, h, l, c = [], [], [], [], []
    with (DATA / f"{sym}_5min.csv").open() as f:
        for row in csv.DictReader(f):
            ts.append(datetime.fromisoformat(row["timestamp"]))
            o.append(float(row["open"]))
            h.append(float(row["high"]))
            l.append(float(row["low"]))
            c.append(float(row["close"]))
    return ts, np.array(o), np.array(h), np.array(l), np.array(c)


def run_symbol(sym: str):
    ts, o, h, l, c = load(sym)
    direction, strength, atr_arr, _, _ = _quant_arrays(c, h, l)
    blocked = np.zeros(len(c), dtype=np.int8)
    e_idx, x_idx, e_px, x_px, side, reason = _simulate(
        c, h, l, direction, strength, atr_arr, blocked,
        float(CONFIG.atr_stop_mult), float(CONFIG.stop_loss_pct),
        float(CONFIG.atr_target_mult), float(CONFIG.take_profit_pct),
        float(CONFIG.confidence_threshold), MAX_HOLD,
    )
    spec = SPECS[sym]
    cost = spec["comm_rt"] + 2 * JUDGE_SLIP_TICKS * spec["tick"] * spec["pt"]
    trades = []
    for i in range(len(e_idx)):
        a, b = int(e_idx[i]), int(x_idx[i])
        pts = (x_px[i] - e_px[i]) * side[i]
        # trades spanning a data gap >30min (roll splice / outage) get flagged;
        # continuous .v.0 series is an unadjusted splice, so P&L across the
        # roll moment is contaminated
        gap = any((ts[j + 1] - ts[j]).total_seconds() > 1800 for j in range(a, b))
        trades.append({
            "symbol": sym,
            "entry_ts": ts[a].isoformat(),
            "year": ts[a].year,
            "session": "RTH" if is_rth(ts[a]) else "overnight",
            "net_usd": float(pts * spec["pt"] - cost),
            "spans_gap": gap,
            "reason": {0: "time", 1: "stop", 2: "target"}[int(reason[i])],
        })
    return trades


def tstat_p(arr: np.ndarray):
    """One-sided t-test p-value for mean > 0 (normal approx on t)."""
    from math import erf, sqrt
    n = len(arr)
    if n < 3 or arr.std(ddof=1) == 0:
        return None, None
    t = arr.mean() / (arr.std(ddof=1) / np.sqrt(n))
    p = 1 - 0.5 * (1 + erf(t / sqrt(2)))
    return round(float(t), 3), round(float(p), 5)


def boot_p(arr: np.ndarray) -> float:
    """Bootstrap P(mean <= 0)."""
    rng = np.random.default_rng(RNG_SEED)
    means = rng.choice(arr, size=(BOOT_N, len(arr)), replace=True).mean(axis=1)
    return round(float((means <= 0).mean()), 5)


def cell(trades):
    arr = np.array([t["net_usd"] for t in trades])
    if len(arr) == 0:
        return {"n": 0}
    t, p = tstat_p(arr)
    gp = arr[arr > 0].sum()
    gl = arr[arr <= 0].sum()
    years = {}
    for tr in trades:
        years.setdefault(tr["year"], []).append(tr["net_usd"])
    yearly = {y: round(sum(v), 2) for y, v in sorted(years.items())}
    pos_years = sum(1 for v in yearly.values() if v > 0)
    return {
        "n": len(arr), "win_pct": round(100 * (arr > 0).mean(), 1),
        "total_usd": round(float(arr.sum()), 2),
        "avg_usd": round(float(arr.mean()), 2),
        "pf": round(float(gp / -gl), 3) if gl < 0 else None,
        "t": t, "p_one_sided": p,
        "p_bootstrap": boot_p(arr) if len(arr) >= 10 else None,
        "yearly_usd": yearly,
        "pct_years_positive": round(100 * pos_years / len(yearly), 1),
    }


def main() -> int:
    missing = [s for s in SPECS if not (DATA / f"{s}_5min.csv").exists()]
    if missing:
        print(f"missing data files for {missing} — run fetch_databento.py "
              "--download first", file=sys.stderr)
        return 1
    results = {"judged_at_slip_ticks": JUDGE_SLIP_TICKS, "cells": {}, "hypotheses": {}}
    by_sym = {}
    for sym in SPECS:
        trades = [t for t in run_symbol(sym) if not t["spans_gap"]]
        by_sym[sym] = trades
        for sess in ("all", "RTH", "overnight"):
            sub = trades if sess == "all" else [t for t in trades if t["session"] == sess]
            results["cells"][f"{sym}_{sess}"] = cell(sub)

    es_on = results["cells"]["ES_overnight"]
    h1 = (es_on.get("n", 0) >= 30
          and es_on["p_one_sided"] is not None and es_on["p_one_sided"] < 0.05
          and es_on["p_bootstrap"] is not None and es_on["p_bootstrap"] < 0.05
          and es_on["pct_years_positive"] >= 60)
    es_all = results["cells"]["ES_all"]
    h2 = bool(es_all.get("total_usd", 0) > 0 and (es_all.get("pf") or 0) >= 1.1)
    mnq_on = results["cells"]["MNQ_overnight"]
    h3 = bool(mnq_on.get("p_one_sided") is not None and mnq_on["p_one_sided"] < 0.05)
    results["hypotheses"] = {
        "H1_es_overnight_edge": "PASS" if h1 else "FAIL",
        "H2_es_all_hours_pf_1.1": "PASS" if h2 else "FAIL",
        "H3_mnq_overnight": "PASS" if h3 else "FAIL",
    }
    out = Path(__file__).resolve().parent / "oos_results.json"
    out.write_text(json.dumps(results, indent=1))
    trades_out = Path(__file__).resolve().parent / "oos_trades.json"
    trades_out.write_text(json.dumps([t for v in by_sym.values() for t in v], indent=1))
    print(json.dumps(results["hypotheses"], indent=1))
    for k, v in results["cells"].items():
        print(f"{k:16} n={v.get('n', 0):6} total=${v.get('total_usd', 0):>12} "
              f"PF={v.get('pf')} t={v.get('t')} p={v.get('p_one_sided')}")
    print(f"full results -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
