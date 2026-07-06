"""Round 13: FOMC announcement reversal (Baglioni & Ribeiro 2022).

Spec frozen in HYPOTHESES.md Round 13, before this data was pulled/joined.

Mechanism: fade the trailing same-clock-time return into the 13:50 ET
pre-statement mark; exit at the RTH 16:00 ET close same day. No overnight
hold — this is Topstep-legal by construction, unlike every entry in the
overnight-drift family (Rounds 2-4, 7-8, 12).

FOMC dates below are the "day 2" (or single-day) date of each REGULARLY
SCHEDULED meeting, hand-verified against the Federal Reserve's own
historical archive (federalreserve.gov/monetarypolicy/fomchistorical{YEAR}.htm)
on 2026-07-06 — not from memory, not from a third-party calendar. Excluded
by design by the same criterion Baglioni & Ribeiro use ("180 SCHEDULED
announcements"): unscheduled/emergency inter-meeting actions, notation
votes, and conference calls with no independent policy Statement of their
own. Excluded here for that reason (with the source page's own label):
  2010  May 9 "Conference Call"        (Statement present, but unscheduled)
  2010  Oct 15 "Conference Call"       (no Statement)
  2011  Aug 1 "Conference Call"        (no Statement)
  2011  Nov 28 "Conference Call"       (no Statement)
  2013  Oct 16 "(unscheduled)"         (no Statement)
  2014  Mar 4 "(unscheduled)"          (no Statement)
  2019  Oct 4 "(unscheduled)"          (balance-sheet operations, not a
                                        rate decision on the normal cadence)
  2020  Mar 2 "(unscheduled) Meeting"  (emergency 50bp cut, COVID)
  2020  Mar 15 "(unscheduled) Meeting" (emergency 100bp cut + QE, COVID)
  2020  Mar 17-18 "(cancelled) Meeting" (superseded by the above two)
  2025  Aug 22 "(notation vote)"       (Jackson Hole framework statement,
                                        not a rate decision)
2020 therefore has 7 scheduled meetings instead of 8 — a real, disclosed
exception, not an error. 2026 is partial (only meetings that have already
occurred as of the registration date, 2026-07-06). 2027 is excluded
entirely: dates are explicitly marked "tentative" by the Fed and are all
in the future relative to this registration.

Usage:  .venv/bin/python oos/round13_fomc_reversal.py
Requires oos/data/{ES,NQ,MES,MNQ}_5min.csv (same files Rounds 2/7/12 use).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np

DATA = Path(__file__).resolve().parent / "data"
OUT = Path(__file__).resolve().parent

# point value, tick size, round-trip commission — same conventions as candidates.py
SPECS = {
    "ES":  {"pt": 50.0, "tick": 0.25, "comm_rt": 4.00},
    "NQ":  {"pt": 20.0, "tick": 0.25, "comm_rt": 4.00},
    "MES": {"pt": 5.0,  "tick": 0.25, "comm_rt": 1.40},
    "MNQ": {"pt": 2.0,  "tick": 0.25, "comm_rt": 1.40},
}
SLIP_TICKS = 1
BOOT_N = 20_000
RNG_SEED = 7

# Announcement-day date (ET calendar date) for every regularly scheduled
# FOMC decision, 2010-2026 (partial). See module docstring for exclusions.
FOMC_DATES: list[str] = [
    # 2010
    "2010-01-27", "2010-03-16", "2010-04-28", "2010-06-23",
    "2010-08-10", "2010-09-21", "2010-11-03", "2010-12-14",
    # 2011
    "2011-01-26", "2011-03-15", "2011-04-27", "2011-06-22",
    "2011-08-09", "2011-09-21", "2011-11-02", "2011-12-13",
    # 2012
    "2012-01-25", "2012-03-13", "2012-04-25", "2012-06-20",
    "2012-08-01", "2012-09-13", "2012-10-24", "2012-12-12",
    # 2013
    "2013-01-30", "2013-03-20", "2013-05-01", "2013-06-19",
    "2013-07-31", "2013-09-18", "2013-10-30", "2013-12-18",
    # 2014
    "2014-01-29", "2014-03-19", "2014-04-30", "2014-06-18",
    "2014-07-30", "2014-09-17", "2014-10-29", "2014-12-17",
    # 2015
    "2015-01-28", "2015-03-18", "2015-04-29", "2015-06-17",
    "2015-07-29", "2015-09-17", "2015-10-28", "2015-12-16",
    # 2016
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15",
    "2016-07-27", "2016-09-21", "2016-11-02", "2016-12-14",
    # 2017
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14",
    "2017-07-26", "2017-09-20", "2017-11-01", "2017-12-13",
    # 2018
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13",
    "2018-08-01", "2018-09-26", "2018-11-08", "2018-12-19",
    # 2019
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19",
    "2019-07-31", "2019-09-18", "2019-10-30", "2019-12-11",
    # 2020 — only 7 (see exclusions above)
    "2020-01-29", "2020-04-29", "2020-06-10", "2020-07-29",
    "2020-09-16", "2020-11-05", "2020-12-16",
    # 2021
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16",
    "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    # 2022
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    # 2023
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026 — partial: only meetings that have occurred as of 2026-07-06
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
]


def load(sym: str) -> dict[date, dict[int, float]]:
    """{calendar_date: {minute_of_day: close}} — minute_of_day = hour*60+minute (ET)."""
    import csv
    from datetime import datetime
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    by_day: dict[date, dict[int, float]] = {}
    path = DATA / f"{sym}_5min.csv"
    if not path.exists():
        return by_day
    with path.open() as f:
        for row in csv.DictReader(f):
            dt = datetime.fromisoformat(row["timestamp"]).astimezone(et)
            by_day.setdefault(dt.date(), {})[dt.hour * 60 + dt.minute] = float(row["close"])
    return by_day


def _nearest_at_or_before(day_bars: dict[int, float], target_min: int, tol: int = 10) -> float | None:
    """Close of the bar at target_min, or the closest bar within `tol` minutes
    before it (5-min bars won't always land exactly on :50)."""
    for m in range(target_min, target_min - tol - 1, -1):
        if m in day_bars:
            return day_bars[m]
    return None


def fomc_reversal_trades(bars: dict[date, dict[int, float]]) -> list[tuple[date, float, float, int]]:
    """One trade per FOMC date: (date, entry_px, exit_px, side).
    side = -1 (SELL) if r24 > 0, +1 (BUY) if r24 < 0, skipped if r24 == 0
    or any required bar is missing."""
    days_sorted = sorted(bars)
    trades = []
    for ds in FOMC_DATES:
        d = date.fromisoformat(ds)
        if d not in bars:
            continue
        entry = _nearest_at_or_before(bars[d], 13 * 60 + 50)
        exit_ = _nearest_at_or_before(bars[d], 16 * 60)
        if entry is None or exit_ is None:
            continue
        # prior trading session's 13:50 mark, for r24
        idx = days_sorted.index(d)
        if idx == 0:
            continue
        prior_day = days_sorted[idx - 1]
        prior_1350 = _nearest_at_or_before(bars[prior_day], 13 * 60 + 50)
        if prior_1350 is None:
            continue
        r24 = entry - prior_1350
        if r24 == 0:
            continue
        side = -1 if r24 > 0 else 1
        trades.append((d, entry, exit_, side))
    return trades


def evaluate(trades: list[tuple[date, float, float, int]], sym: str) -> dict:
    spec = SPECS[sym]
    cost = spec["comm_rt"] + 2 * SLIP_TICKS * spec["tick"] * spec["pt"]
    net = np.array([(xp - ep) * side * spec["pt"] - cost for _, ep, xp, side in trades])
    if len(net) == 0:
        return {"n": 0}
    t = p = None
    if len(net) > 2 and net.std(ddof=1) > 0:
        from math import erf, sqrt
        t = float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))))
        p = 1 - 0.5 * (1 + erf(t / sqrt(2)))
    rng = np.random.default_rng(RNG_SEED)
    bp = float((rng.choice(net, size=(BOOT_N, len(net)), replace=True).mean(axis=1) <= 0).mean())
    gp, gl = net[net > 0].sum(), net[net <= 0].sum()
    yearly: dict[int, float] = {}
    for (d, _, _, _), pnl in zip(trades, net):
        yearly[d.year] = yearly.get(d.year, 0.0) + pnl
    pos_years = sum(1 for x in yearly.values() if x > 0)
    return {
        "n": int(len(net)), "win_pct": round(100 * float((net > 0).mean()), 1),
        "total_usd": round(float(net.sum()), 2), "avg_usd": round(float(net.mean()), 2),
        "pf": round(float(gp / -gl), 3) if gl < 0 else None,
        "t": round(t, 3) if t is not None else None,
        "p_one_sided": round(p, 5) if p is not None else None,
        "p_bootstrap": round(bp, 5),
        "yearly_usd": {str(y): round(x, 2) for y, x in sorted(yearly.items())},
        "pct_years_positive": round(100 * pos_years / len(yearly), 1) if yearly else 0.0,
    }


def pooled_evaluate(per_symbol_trades: dict[str, list]) -> dict:
    """Point-normalize each symbol's P&L (divide by point value) and pool
    across symbols on the same FOMC date, per Round 7's method — frozen in
    the pre-registration, not chosen after seeing results."""
    cost_pts = {sym: SPECS[sym]["comm_rt"] / SPECS[sym]["pt"] + 2 * SLIP_TICKS * SPECS[sym]["tick"]
                for sym in SPECS}
    net_pts = []
    yearly: dict[int, float] = {}
    for sym, trades in per_symbol_trades.items():
        for d, ep, xp, side in trades:
            pnl_pts = (xp - ep) * side - cost_pts[sym]
            net_pts.append(pnl_pts)
            yearly[d.year] = yearly.get(d.year, 0.0) + pnl_pts
    net = np.array(net_pts)
    if len(net) == 0:
        return {"n": 0}
    from math import erf, sqrt
    t = p = None
    if len(net) > 2 and net.std(ddof=1) > 0:
        t = float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))))
        p = 1 - 0.5 * (1 + erf(t / sqrt(2)))
    rng = np.random.default_rng(RNG_SEED)
    bp = float((rng.choice(net, size=(BOOT_N, len(net)), replace=True).mean(axis=1) <= 0).mean())
    gp, gl = net[net > 0].sum(), net[net <= 0].sum()
    pos_years = sum(1 for x in yearly.values() if x > 0)
    return {
        "n": int(len(net)), "win_pct": round(100 * float((net > 0).mean()), 1),
        "total_pts": round(float(net.sum()), 2), "avg_pts": round(float(net.mean()), 4),
        "pf": round(float(gp / -gl), 3) if gl < 0 else None,
        "t": round(t, 3) if t is not None else None,
        "p_one_sided": round(p, 5) if p is not None else None,
        "p_bootstrap": round(bp, 5),
        "yearly_pts": {str(y): round(x, 2) for y, x in sorted(yearly.items())},
        "pct_years_positive": round(100 * pos_years / len(yearly), 1) if yearly else 0.0,
    }


def passes(cell: dict) -> bool:
    return bool(cell.get("n", 0) >= 400 and (cell.get("pf") or 0) >= 1.10
                and cell.get("p_one_sided") is not None and cell["p_one_sided"] < 0.05
                and cell.get("p_bootstrap") is not None and cell["p_bootstrap"] < 0.05
                and cell.get("pct_years_positive", 0) >= 60)


def main() -> int:
    per_symbol_trades = {}
    per_symbol_cells = {}
    for sym in SPECS:
        bars = load(sym)
        if not bars:
            print(f"WARN: no data file for {sym} ({DATA / f'{sym}_5min.csv'}) — skipping")
            continue
        trades = fomc_reversal_trades(bars)
        per_symbol_trades[sym] = trades
        per_symbol_cells[sym] = evaluate(trades, sym)

    pooled = pooled_evaluate(per_symbol_trades)
    verdict = "PASS" if passes(pooled) else "FAIL"
    out = {
        "verdict": verdict,
        "pooled": pooled,
        "per_symbol": per_symbol_cells,
        "fomc_dates_used": len(FOMC_DATES),
        "note": "pooled = point-normalized ES+NQ+MES+MNQ on the same FOMC dates (Round 7 method), "
                "frozen in HYPOTHESES.md Round 13 before this file was run.",
    }
    (OUT / "round13_fomc_reversal_results.json").write_text(json.dumps(out, indent=1))
    print(json.dumps({"verdict": verdict, "pooled": pooled}, indent=1))
    for sym, cell in per_symbol_cells.items():
        print(f"{sym:4} n={cell.get('n', 0):4} total=${cell.get('total_usd', 0):>10} "
              f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
              f"yrs+={cell.get('pct_years_positive')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
