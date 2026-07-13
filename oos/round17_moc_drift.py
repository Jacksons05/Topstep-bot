"""Round 17 -- A2: MOC / cash-close imbalance drift (see oos/HYPOTHESES.md).

Directional ENTRY edge, judged on the standard harness bar (n>=200, PF>=1.15,
one-sided p<0.05 by t AND 20k bootstrap seed 7, >=60% years positive, net of the
ES 1-tick round-trip cost). Distinct from the falsified gamma-direction mechanism.

DATA CONTRACT (what makes this runnable):
  oos/data/moc_imbalance.csv  with columns:
    date            YYYY-MM-DD (session date, ET)
    imbalance_usd   net equity MOC imbalance at ~15:50 ET dissemination ($, signed
                    +buy / -sell; NYSE + Nasdaq summed)
    es_1550         ES price at the 15:50 ET bar close
    es_1558         ES price at the 15:58 ET bar close (exit)
One row per session. Until this file exists the round is DATA-BLOCKED (paid feed
or forward capture -- see HYPOTHESES.md Round 17). numpy + stdlib only.

Run: <py> oos/round17_moc_drift.py
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DATA = HERE / "data" / "moc_imbalance.csv"
RESULTS = HERE / "round17_results.json"

BOOT_N, RNG_SEED = 20_000, 7
ES_PT, ES_TICK, ES_COMM = 50.0, 0.25, 4.00
RT_COST = ES_COMM + 2 * 1 * ES_TICK * ES_PT      # $29.00
TERCILE = 2.0 / 3.0                              # top-tercile |imbalance| gate
TRAIL = 60                                        # trailing sessions for the gate


def evaluate(net, ts_years):
    """Harness-standard metric dict on a net-USD trade array (see backtest_oos.cell)."""
    arr = np.asarray(net, float)
    n = len(arr)
    if n == 0:
        return {"n": 0}
    sd = arr.std(ddof=1)
    t = float(arr.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0
    p_t = 1 - 0.5 * (1 + math.erf(t / math.sqrt(2)))
    rng = np.random.default_rng(RNG_SEED)
    means = rng.choice(arr, size=(BOOT_N, n), replace=True).mean(axis=1)
    p_boot = float((means <= 0).mean())
    gp = arr[arr > 0].sum(); gl = arr[arr <= 0].sum()
    yr = {}
    for y, v in zip(ts_years, arr):
        yr.setdefault(y, 0.0)
        yr[y] += v
    pos_years = sum(1 for v in yr.values() if v > 0)
    return {
        "n": n, "win_pct": round(100 * (arr > 0).mean(), 1),
        "total_usd": round(float(arr.sum()), 2), "avg_usd": round(float(arr.mean()), 2),
        "pf": round(float(gp / -gl), 3) if gl < 0 else None,
        "t": round(t, 3), "p_one_sided": round(p_t, 5), "p_bootstrap": round(p_boot, 5),
        "pct_years_positive": round(100 * pos_years / len(yr), 1),
    }


def passes(c):
    return (c.get("n", 0) >= 200 and (c.get("pf") or 0) >= 1.15
            and (c.get("p_one_sided") or 1) < 0.05 and (c.get("p_bootstrap") or 1) < 0.05
            and (c.get("pct_years_positive") or 0) >= 60)


def build_trades(rows):
    """rows: list of dict(date, imbalance_usd, es_1550, es_1558). Returns
    (net_usd_array, years) applying the frozen A2 rule with the trailing gate."""
    imb = np.array([r["imbalance_usd"] for r in rows], float)
    e50 = np.array([r["es_1550"] for r in rows], float)
    e58 = np.array([r["es_1558"] for r in rows], float)
    yrs = [r["date"][:4] for r in rows]
    net, tyr = [], []
    for i in range(TRAIL, len(rows)):
        gate = np.quantile(np.abs(imb[i - TRAIL:i]), TERCILE)
        if abs(imb[i]) < gate or imb[i] == 0:
            continue
        side = 1 if imb[i] > 0 else -1
        pnl = (e58[i] - e50[i]) * side * ES_PT - RT_COST
        net.append(pnl); tyr.append(yrs[i])
    return np.array(net, float), tyr


def main():
    print("=" * 70)
    print(" ROUND 17 -- A2 MOC / cash-close imbalance drift")
    print("=" * 70)
    if not DATA.exists():
        msg = {"status": "DATA-BLOCKED",
               "needs": str(DATA),
               "columns": ["date", "imbalance_usd", "es_1550", "es_1558"],
               "how": "paid NYSE/Nasdaq MOC-imbalance history, or forward-capture "
                      ">= ~200 sessions (HYPOTHESES.md Round 17)."}
        print(f"  STATUS: DATA-BLOCKED -- {DATA} not present.")
        print(f"  Provide CSV cols {msg['columns']}, then re-run.")
        RESULTS.write_text(json.dumps(msg, indent=2))
        return
    with open(DATA, newline="") as fh:
        rows = [{"date": r["date"], "imbalance_usd": float(r["imbalance_usd"]),
                 "es_1550": float(r["es_1550"]), "es_1558": float(r["es_1558"])}
                for r in csv.DictReader(fh)]
    net, tyr = build_trades(rows)
    cell = evaluate(net, tyr)
    verdict = "PASS" if passes(cell) else "FAIL"
    print(f"  n={cell.get('n')} PF={cell.get('pf')} t={cell.get('t')} "
          f"p_t={cell.get('p_one_sided')} p_boot={cell.get('p_bootstrap')} "
          f"years+={cell.get('pct_years_positive')}%  -> {verdict}")
    RESULTS.write_text(json.dumps({"verdict": verdict, "cell": cell}, indent=2))
    print("=" * 70)


if __name__ == "__main__":
    main()
