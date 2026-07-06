"""Round 9: SPY UW options-flow day score -> next-day ES (18:00 entry, 16:00 exit).

Spec frozen in HYPOTHESES.md Round 9. Flow score = JARVIS backtest_flow.py
day_flow_score formula, frozen. Pulls as much net-prem-ticks history as UW
serves (weekdays back from 2026-06-05, stops after 30 consecutive empty days).

Run from Trading-Bot venv (needs UW client):
  ~/Claude/Trading-Bot/.venv312/bin/python oos/round9_uwflow.py
"""
import csv
import json
import sys
from datetime import date, datetime, timedelta
from math import erf, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
TB = Path("/Users/jacksonsheehan/Claude/Trading-Bot")
sys.path.insert(0, str(TB))

import httpx  # noqa: E402
from backtest_flow import UW_BASE, _uw_day_flow  # noqa: E402  (frozen formula)
from config import CONFIG  # noqa: E402

ET = ZoneInfo("America/New_York")
END = date(2026, 6, 5)
MAX_EMPTY_STREAK = 30
PT, TICK, COMM = 50.0, 0.25, 4.00


def pull_scores():
    http = httpx.Client(timeout=15, headers={
        "Authorization": f"Bearer {CONFIG.uw_api_token}", "Accept": "application/json"})
    scores = {}
    d = END
    empty = 0
    while empty < MAX_EMPTY_STREAK:
        if d.weekday() < 5:
            s = _uw_day_flow(http, "SPY", d.isoformat())
            if s is None:
                empty += 1
            else:
                empty = 0
                scores[d.isoformat()] = s
        d -= timedelta(days=1)
    http.close()
    return scores


def load_es():
    bars = {}
    with (HERE / "data" / "ES_5min.csv").open() as f:
        for row in csv.DictReader(f):
            dt = datetime.fromisoformat(row["timestamp"]).astimezone(ET)
            bars.setdefault(dt.date(), []).append((dt, float(row["close"])))
    return bars


def main() -> int:
    scores = pull_scores()
    print(f"flow scores pulled: {len(scores)} days "
          f"({min(scores) if scores else '-'} .. {max(scores) if scores else '-'})", flush=True)
    bars = load_es()
    days = sorted(bars)
    net = []
    rows = []
    for i, d in enumerate(days[:-1]):
        s = scores.get(d.isoformat())
        if s is None or abs(s) <= 0.15:
            continue
        side = 1 if s > 0 else -1
        entry = next((px for dt, px in bars[d] if dt.hour == 18 and dt.minute == 0), None)
        nxt = days[i + 1]
        exit_px = next((px for dt, px in bars[nxt] if dt.hour == 16 and dt.minute == 0), None)
        if entry is None or exit_px is None:
            continue
        pnl = (exit_px - entry) * side * PT - COMM - 2 * TICK * PT
        net.append(pnl)
        rows.append({"day": d.isoformat(), "score": round(s, 3), "side": side, "net": round(pnl, 2)})
    net = np.array(net)
    out = {"n": int(len(net))}
    if len(net) > 2 and net.std(ddof=1) > 0:
        t = float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))))
        p = 1 - 0.5 * (1 + erf(t / sqrt(2)))
        rng = np.random.default_rng(7)
        bp = float((rng.choice(net, size=(20000, len(net)), replace=True).mean(axis=1) <= 0).mean())
        gp, gl = net[net > 0].sum(), net[net <= 0].sum()
        pf = float(gp / -gl) if gl < 0 else None
        ok = bool(len(net) >= 100 and pf and pf >= 1.10 and p < 0.05 and bp < 0.05)
        out.update({"verdict": "PASS" if ok else "FAIL",
                    "total_usd": round(float(net.sum()), 2), "avg_usd": round(float(net.mean()), 2),
                    "pf": round(pf, 3) if pf else None, "t": round(t, 3),
                    "p_one_sided": round(p, 5), "p_bootstrap": round(bp, 5),
                    "win_pct": round(100 * float((net > 0).mean()), 1)})
    else:
        out["verdict"] = "FAIL (insufficient trades)"
    (HERE / "round9_uwflow_results.json").write_text(json.dumps({"summary": out, "trades": rows}, indent=1))
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
