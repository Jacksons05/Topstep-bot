"""Round 18: dealer net-gamma-level intraday reversal -> ES (judged) / NQ (exploratory).

Spec frozen in HYPOTHESES.md Round 18 BEFORE this script computed any P&L.
Signal source: UW `/api/stock/{ticker}/spot-exposures` (per-1min gamma-per-
1%-move, OI-based field), NOT the EOD-only `/greek-exposure` endpoint Round 6
used, and NOT the `/market-tide` endpoint Round 14 used.

Confirmed tier: current UW subscription only has ~90 trading days of history
(earliest 2026-02-26) -> EXPLORATORY ONLY, no PASS from this round is
actionable. Run anyway, per the frozen spec, and report honestly.

Usage:
  .venv/bin/python oos/round18_gamma_reversal.py --pull
  .venv/bin/python oos/round18_gamma_reversal.py
"""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import CONFIG  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
OUT = Path(__file__).resolve().parent
DATA = OUT / "data"
CACHE_PATH = OUT / "round18_gamma_scores.json"
RESULTS_PATH = OUT / "round18_results.json"
UW_BASE = "https://api.unusualwhales.com"
SPOT_PATH = "/api/stock/{ticker}/spot-exposures"

# underlier -> (futures root, bar file, judged?)
LEGS = {
    "SPX": {"root": "ES", "file": "ES_5min.csv", "judged": True, "comm_rt": 4.00},
    "NDX": {"root": "NQ", "file": "NQ_5min.csv", "judged": False, "comm_rt": 4.00},
}
TICK = 0.25
PT = {"ES": 50.0, "NQ": 20.0}
SLIP_TICKS = 1
TERCILE_WINDOW_DAYS = 5  # trailing trading days, frozen
DIR_LOOKBACK_MIN = 10  # trailing move used to pick fade direction, frozen
HOLD_MINUTES = [10, 30, 60]  # primary=10 (judged), 30/60 exploratory-reported
FLATTEN_MIN = 15 * 60 + 59  # 15:59 ET hard cap, minutes-since-midnight
ENTRY_WINDOW = (9 * 60 + 30, 15 * 60 + 0)  # 09:30-15:00 ET
BOOT_N = 20_000
RNG_SEED = 7
EARLIEST = date(2026, 2, 26)  # confirmed floor of this UW tier, 2026-07-09


def _client() -> httpx.Client:
    if not CONFIG.uw_api_key:
        raise SystemExit("UW_API_KEY not set.")
    return httpx.Client(timeout=15.0, headers={"Authorization": f"Bearer {CONFIG.uw_api_key}"})


def _weekdays(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def pull() -> int:
    http = _client()
    cache: dict[str, dict[str, list]] = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
    today = date.today()
    n_pulled = 0
    for ticker in LEGS:
        cache.setdefault(ticker, {})
        for d in _weekdays(EARLIEST, today - timedelta(days=1)):
            key = d.isoformat()
            if key in cache[ticker]:
                continue
            try:
                resp = http.get(
                    f"{UW_BASE}{SPOT_PATH.format(ticker=ticker)}",
                    params={"date": key},
                )
                if resp.status_code != 200:
                    cache[ticker][key] = []
                    time.sleep(0.3)
                    continue
                rows = resp.json().get("data", []) or []
                # keep only what the frozen rule needs
                slim = [
                    {"t": r["time"], "g": float(r["gamma_per_one_percent_move_oi"])}
                    for r in rows
                    if r.get("gamma_per_one_percent_move_oi") is not None
                ]
                cache[ticker][key] = slim
                n_pulled += 1
            except Exception:  # noqa: BLE001
                cache[ticker][key] = []
            time.sleep(0.3)
            if n_pulled % 25 == 0 and n_pulled > 0:
                CACHE_PATH.write_text(json.dumps(cache))
    CACHE_PATH.write_text(json.dumps(cache))
    http.close()
    print(f"pulled {n_pulled} new ticker-days -> {CACHE_PATH}")
    return 0


def _load_gamma_series(ticker: str, cache: dict) -> list[tuple[datetime, float]]:
    out = []
    for day_key, rows in cache.get(ticker, {}).items():
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


LEGS_FILE = {v["root"]: v["file"] for v in LEGS.values()}


def _bar_at_or_after(day_bars: dict[int, float], target_min: int, tol: int = 10) -> tuple[float | None, int | None]:
    for m in range(target_min, target_min + tol + 1):
        if m in day_bars:
            return day_bars[m], m
    return None, None


def backtest() -> int:
    if not CACHE_PATH.exists():
        raise SystemExit("no cached gamma scores -- run --pull first")
    cache = json.loads(CACHE_PATH.read_text())

    all_cells: dict[str, dict] = {}
    for ticker, leg in LEGS.items():
        root = leg["root"]
        series = _load_gamma_series(ticker, cache)
        bars = _load_bars(root)
        if not series or not bars:
            print(f"WARN: no data for {ticker}/{root} -- skipping")
            continue

        # trailing 5-trading-day pool, recomputed causally at each snapshot
        by_day: dict[date, list[float]] = {}
        for ts, g in series:
            by_day.setdefault(ts.date(), []).append(g)
        trading_days = sorted(by_day)

        trades_by_hold: dict[int, list] = {h: [] for h in HOLD_MINUTES}
        open_until: dict[int, datetime | None] = {h: None for h in HOLD_MINUTES}

        for i, (ts, g) in enumerate(series):
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
            if g < hi:
                continue  # only top-tercile (strongly positive net gamma) entries per frozen rule

            day_bars = bars.get(d)
            if not day_bars:
                continue
            cur_px, cur_m = _bar_at_or_after(day_bars, minute, tol=10)
            past_px, past_m = _bar_at_or_after(day_bars, minute - DIR_LOOKBACK_MIN, tol=10)
            if cur_px is None or past_px is None or cur_m == past_m:
                continue
            if cur_px == past_px:
                continue
            side = -1 if cur_px > past_px else 1  # fade the trailing 10min move

            for hold in HOLD_MINUTES:
                if open_until[hold] is not None and ts < open_until[hold]:
                    continue  # position already open on this leg/hold, skip overlap
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
    print("\nEXPLORATORY ONLY -- UW history is 90 trading days (<1yr). No cell above "
          "is an actionable PASS regardless of these numbers. See HYPOTHESES.md Round 18.")
    return 0


def main() -> int:
    if "--pull" in sys.argv[1:]:
        return pull()
    return backtest()


if __name__ == "__main__":
    raise SystemExit(main())
