"""Round 27: UW dealer-gamma NEGATIVE-tercile leg (momentum-following) ->
ES (judged) / NQ (exploratory).

Spec frozen in HYPOTHESES.md Round 27 BEFORE this script computed any P&L.
This is the untested mirror of Round 18 (which froze and judged the
TOP-tercile FADE only; the bottom tercile was explicitly stood aside on,
never traded, in that round). Round 27 is that never-tested leg, registered
as its own hypothesis per Round 18's own text ("a sign-flip test... would
need its own pre-registration"), not a rescue of Round 18's dead fade cell.

Everything is identical to oos/round18_gamma_reversal.py EXCEPT:
  - entry tercile: BOTTOM (strongly negative net gamma), not top
  - direction: WITH the trailing 10-min move (momentum), not against it (fade)
Signal source, cache format, hold times, costs, and stats kernel are shared
verbatim with Round 18 -- this script reuses Round 18's own gamma-score cache
(oos/round18_gamma_scores.json) and does not re-pull UW data.

Confirmed tier (inherited from Round 18, re-check before trusting a number):
current UW subscription had ~90-120 trading days of history as of 2026-07
(< 1 calendar year) -> EXPLORATORY ONLY, no PASS from this round is
actionable until the accumulating `com.jarvis.uwcapture` history clears a
full year (~Oct 2026). Run anyway, per the frozen spec, and report honestly.

Usage:
  .venv/bin/python oos/round27_gamma_momentum.py
    (reads the existing oos/round18_gamma_scores.json cache; run
    oos/round18_gamma_reversal.py --pull first if that cache is stale/absent)
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ET = ZoneInfo("America/New_York")
OUT = Path(__file__).resolve().parent
DATA = OUT / "data"
CACHE_PATH = OUT / "round18_gamma_scores.json"   # shared with Round 18, no new pull
RESULTS_PATH = OUT / "round27_results.json"

# underlier -> (futures root, bar file, judged?) -- identical to Round 18
LEGS = {
    "SPX": {"root": "ES", "file": "ES_5min.csv", "judged": True, "comm_rt": 4.00},
    "NDX": {"root": "NQ", "file": "NQ_5min.csv", "judged": False, "comm_rt": 4.00},
}
TICK = 0.25
PT = {"ES": 50.0, "NQ": 20.0}
SLIP_TICKS = 1
TERCILE_WINDOW_DAYS = 5  # trailing trading days, frozen (matches Round 18)
DIR_LOOKBACK_MIN = 10    # trailing move that sets momentum direction, frozen
HOLD_MINUTES = [10, 30, 60]  # primary=10 (judged), 30/60 exploratory-reported
FLATTEN_MIN = 15 * 60 + 59   # 15:59 ET hard cap, minutes-since-midnight
ENTRY_WINDOW = (9 * 60 + 30, 15 * 60 + 0)  # 09:30-15:00 ET
BOOT_N = 20_000
RNG_SEED = 7
LEGS_FILE = {v["root"]: v["file"] for v in LEGS.values()}


def _load_gamma_series(ticker: str, cache: dict) -> list[tuple[datetime, float]]:
    out = []
    for _day_key, rows in cache.get(ticker, {}).items():
        for r in rows:
            ts = datetime.fromisoformat(r["t"].replace("Z", "+00:00")).astimezone(ET)
            out.append((ts, r["g"]))
    out.sort(key=lambda x: x[0])
    return out


def _load_bars(root: str) -> dict[date, dict[int, float]]:
    path = DATA / LEGS_FILE[root]
    by_day: dict[date, dict[int, float]] = {}
    if not path.exists():
        return by_day
    with path.open() as f:
        for row in csv.DictReader(f):
            dt = datetime.fromisoformat(row["timestamp"]).astimezone(ET)
            by_day.setdefault(dt.date(), {})[dt.hour * 60 + dt.minute] = float(row["close"])
    return by_day


def _bar_at_or_after(day_bars: dict[int, float], target_min: int, tol: int = 10) -> tuple[float | None, int | None]:
    for m in range(target_min, target_min + tol + 1):
        if m in day_bars:
            return day_bars[m], m
    return None, None


def backtest() -> int:
    if not CACHE_PATH.exists():
        raise SystemExit(
            "no cached gamma scores -- run "
            "`.venv/bin/python oos/round18_gamma_reversal.py --pull` first "
            "(Round 27 reuses that cache; it does not pull its own)."
        )
    cache = json.loads(CACHE_PATH.read_text())

    all_cells: dict[str, dict] = {}
    for ticker, leg in LEGS.items():
        root = leg["root"]
        series = _load_gamma_series(ticker, cache)
        bars = _load_bars(root)
        if not series or not bars:
            print(f"WARN: no data for {ticker}/{root} -- skipping")
            continue

        by_day: dict[date, list[float]] = {}
        for ts, g in series:
            by_day.setdefault(ts.date(), []).append(g)
        trading_days = sorted(by_day)

        trades_by_hold: dict[int, list] = {h: [] for h in HOLD_MINUTES}
        open_until: dict[int, datetime | None] = {h: None for h in HOLD_MINUTES}

        for ts, g in series:
            d = ts.date()
            di = trading_days.index(d)
            if di < TERCILE_WINDOW_DAYS:
                continue
            trailing_days = trading_days[di - TERCILE_WINDOW_DAYS:di]
            pool = [v for dd in trailing_days for v in by_day[dd]]
            if len(pool) < 30:
                continue
            lo, hi = np.percentile(pool, [33.3, 66.7])

            minute = ts.hour * 60 + ts.minute
            if not (ENTRY_WINDOW[0] <= minute <= ENTRY_WINDOW[1]):
                continue
            if g > lo:
                continue  # only bottom-tercile (strongly negative net gamma) entries, frozen

            day_bars = bars.get(d)
            if not day_bars:
                continue
            cur_px, cur_m = _bar_at_or_after(day_bars, minute, tol=10)
            past_px, past_m = _bar_at_or_after(day_bars, minute - DIR_LOOKBACK_MIN, tol=10)
            if cur_px is None or past_px is None or cur_m == past_m:
                continue
            if cur_px == past_px:
                continue
            side = 1 if cur_px > past_px else -1  # WITH the trailing 10min move (momentum)

            for hold in HOLD_MINUTES:
                if open_until[hold] is not None and ts < open_until[hold]:
                    continue
                exit_min = min(minute + hold, FLATTEN_MIN)
                exit_px, exit_m = _bar_at_or_after(day_bars, exit_min, tol=10)
                if exit_px is None:
                    continue
                trades_by_hold[hold].append((d, cur_px, exit_px, side))
                open_until[hold] = ts + timedelta(minutes=hold)

        cost_pts = leg["comm_rt"] / PT[root] + 2 * SLIP_TICKS * TICK
        for hold, trades in trades_by_hold.items():
            net_pts, yearly = [], {}
            for d, ep, xp, side in trades:
                pnl = (xp - ep) * side - cost_pts
                net_pts.append(pnl)
                yearly[d.year] = yearly.get(d.year, 0.0) + pnl
            net = np.array(net_pts)
            cell: dict = {"n": int(len(net)), "judged": leg["judged"] and hold == 10}
            if len(net) > 2 and net.std(ddof=1) > 0:
                from math import erf, sqrt
                t = float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))))
                p = 1 - 0.5 * (1 + erf(t / sqrt(2)))
                rng = np.random.default_rng(RNG_SEED)
                bp = float((rng.choice(net, size=(BOOT_N, len(net)), replace=True).mean(axis=1) <= 0).mean())
                gp, gl = net[net > 0].sum(), net[net <= 0].sum()
                cell.update({
                    "pf": round(float(gp / -gl), 3) if gl < 0 else None,
                    "t": round(t, 3), "p_one_sided": round(p, 5), "p_bootstrap": round(bp, 5),
                    "win_pct": round(100 * float((net > 0).mean()), 1),
                    "yearly_pts": {str(y): round(x, 2) for y, x in sorted(yearly.items())},
                })
            all_cells[f"{root}_hold{hold}"] = cell

    print(json.dumps(all_cells, indent=1))
    RESULTS_PATH.write_text(json.dumps(all_cells, indent=1))
    print("\nEXPLORATORY ONLY -- UW gamma history is <1yr as of registration. No "
          "cell above is an actionable PASS regardless of these numbers. See "
          "HYPOTHESES.md Round 27.")
    return 0


def main() -> int:
    return backtest()


if __name__ == "__main__":
    raise SystemExit(main())
