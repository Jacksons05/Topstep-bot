"""Round 14: UW market-tide daily options positioning -> next-session ES/NQ.

Spec frozen in HYPOTHESES.md Round 14 — diagnostic-first, before any P&L is
computed. This is a DIFFERENT data source/mechanism from Round 9 (single-name
SPY flow-alerts, dead at n=55): this uses /api/market/market-tide, which is
market-wide and accepts a historical `date` parameter.

Three phases, run in this order, every time:

  1. `--diagnostic`   Pure data-availability check. No P&L. Probes market-tide
                       at ~20-trading-day intervals going back from today and
                       reports the first date the response goes empty/errors.
                       This determines which PASS-bar tier applies (see
                       HYPOTHESES.md Round 14) — record its output honestly
                       even if the history is short.

  2. `--pull`          Backfill day_score for every trading day back to the
                       diagnostic's usable-history boundary (or a --since
                       date), caching to round14_tide_scores.json so re-runs
                       don't re-hit the API. Respects UW's ~120 req/min limit
                       with a conservative pacing sleep.

  3. (default)         Join cached day_scores to oos/data/{SYM}_5min.csv,
                       apply the FROZEN trade rule from HYPOTHESES.md Round 14
                       (trailing-60-day tercile threshold, RTH-only, no
                       overnight hold), and report pooled + per-symbol stats
                       using the same evaluate() convention as every other
                       round.

Usage:
  .venv/bin/python oos/round14_uw_tide.py --diagnostic
  .venv/bin/python oos/round14_uw_tide.py --pull --since 2023-01-01
  .venv/bin/python oos/round14_uw_tide.py
"""
from __future__ import annotations

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
OUT = Path(__file__).resolve().parent
DATA = OUT / "data"
CACHE_PATH = OUT / "round14_tide_scores.json"
UW_BASE = "https://api.unusualwhales.com"
TIDE_PATH = "/api/market/market-tide"

SPECS = {
    "ES":  {"pt": 50.0, "tick": 0.25, "comm_rt": 4.00},
    "NQ":  {"pt": 20.0, "tick": 0.25, "comm_rt": 4.00},
    "MES": {"pt": 5.0,  "tick": 0.25, "comm_rt": 1.40},
    "MNQ": {"pt": 2.0,  "tick": 0.25, "comm_rt": 1.40},
}
SLIP_TICKS = 1
BOOT_N = 20_000
RNG_SEED = 7
TERCILE_WINDOW = 60  # trailing trading days, frozen — never look-ahead / full-sample


def _client() -> httpx.Client:
    if not CONFIG.uw_api_key:
        raise SystemExit("UW_API_KEY is not set — export it or put it in .env before running.")
    return httpx.Client(
        timeout=15.0,
        headers={"Authorization": f"Bearer {CONFIG.uw_api_key}", "Accept": "application/json"},
    )


def _weekdays_back(n: int, from_date: date | None = None):
    """Yield the last n weekdays (not holiday-aware — good enough for a probe)."""
    d = from_date or date.today()
    out = []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def _fetch_tide_day(http: httpx.Client, d: date) -> list[dict] | None:
    try:
        resp = http.get(f"{UW_BASE}{TIDE_PATH}", params={"date": d.isoformat()})
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", [])
        return data or None
    except Exception:  # noqa: BLE001
        return None


def diagnostic() -> int:
    """Data-availability check ONLY — no P&L computed here. Probe every ~20
    trading days going back up to 5 years and report where data stops."""
    http = _client()
    print("UW market-tide diagnostic — probing historical depth (no P&L computed)\n")
    probe_dates = [_weekdays_back(1, date.today() - timedelta(days=i))[0]
                   for i in range(0, 5 * 252, 20)]
    last_good = None
    first_empty = None
    sample_shapes = []
    for i, d in enumerate(probe_dates):
        data = _fetch_tide_day(http, d)
        ok = data is not None and len(data) > 0
        if ok:
            last_good = d
            if len(sample_shapes) < 3:
                sample_shapes.append({"date": d.isoformat(), "n_ticks": len(data),
                                       "first_tick": data[0], "last_tick": data[-1]})
        elif first_empty is None:
            first_empty = d
        print(f"  probe {d.isoformat()}  {'OK  n_ticks=' + str(len(data)) if ok else 'EMPTY/ERROR'}")
        time.sleep(0.3)  # ~200/min pace, well under the 120/min per-minute cap in bursts
    http.close()

    span_days = (date.today() - last_good).days if last_good else None
    print("\n--- diagnostic result (record this in HYPOTHESES.md regardless of outcome) ---")
    print(f"  furthest confirmed usable date: {last_good}")
    print(f"  approx usable span: {span_days} calendar days" if span_days else "  no usable data found")
    print("  sample tick shapes (to decide last-tick vs sum-of-day scoring, per the frozen "
          "rule in HYPOTHESES.md Round 14 — decide from shape only, never from outcomes):")
    for s in sample_shapes:
        print(f"    {s['date']}: {s['n_ticks']} ticks | first={s['first_tick']} | last={s['last_tick']}")
    print("\nPASS-bar tier that applies (from HYPOTHESES.md Round 14):")
    if span_days is None:
        print("  NO USABLE HISTORY — stop; rely on forward/live logging only.")
    elif span_days >= 365:
        print("  >= 1 year available -> full test with n>=200 pooled PASS bar.")
    elif span_days >= 90:
        print("  3-12 months available -> EXPLORATORY ONLY, no PASS is actionable yet.")
    else:
        print("  < 3 months -> stop; rely on forward/live logging only (same as Round 9).")
    return 0


def pull(since: date) -> int:
    http = _client()
    cache: dict[str, float] = {}
    if CACHE_PATH.exists():
        cache = json.loads(CACHE_PATH.read_text())
    d = date.today() - timedelta(days=1)
    n_pulled = 0
    while d >= since:
        if d.weekday() < 5 and d.isoformat() not in cache:
            ticks = _fetch_tide_day(http, d)
            if ticks:
                last = ticks[-1]
                # FROZEN scoring choice — see HYPOTHESES.md Round 14 step 2.
                # Uses the last tick's net_call_premium - net_put_premium as
                # the end-of-day reading. If the diagnostic showed this field
                # is a per-interval delta rather than a running total, switch
                # this to sum(...) over `ticks` instead — decide from the
                # diagnostic's printed shapes, not from any backtest result.
                score = float(last.get("net_call_premium", 0) or 0) - float(last.get("net_put_premium", 0) or 0)
                cache[d.isoformat()] = score
                n_pulled += 1
            time.sleep(0.3)
        d -= timedelta(days=1)
        if n_pulled % 50 == 0 and n_pulled > 0:
            CACHE_PATH.write_text(json.dumps(cache, indent=1, sort_keys=True))
    CACHE_PATH.write_text(json.dumps(cache, indent=1, sort_keys=True))
    http.close()
    print(f"pulled {n_pulled} new day_scores; cache now has {len(cache)} days -> {CACHE_PATH}")
    return 0


def _load_bars(sym: str) -> dict[date, dict[int, float]]:
    import csv
    path = DATA / f"{sym}_5min.csv"
    by_day: dict[date, dict[int, float]] = {}
    if not path.exists():
        return by_day
    with path.open() as f:
        for row in csv.DictReader(f):
            dt = datetime.fromisoformat(row["timestamp"]).astimezone(ET)
            by_day.setdefault(dt.date(), {})[dt.hour * 60 + dt.minute] = float(row["close"])
    return by_day


def _bar_at_or_after(day_bars: dict[int, float], target_min: int, tol: int = 10) -> float | None:
    for m in range(target_min, target_min + tol + 1):
        if m in day_bars:
            return day_bars[m]
    return None


def _bar_at_or_before(day_bars: dict[int, float], target_min: int, tol: int = 10) -> float | None:
    for m in range(target_min, target_min - tol - 1, -1):
        if m in day_bars:
            return day_bars[m]
    return None


def backtest() -> int:
    if not CACHE_PATH.exists():
        raise SystemExit("no cached scores — run --pull first")
    scores = {date.fromisoformat(k): v for k, v in json.loads(CACHE_PATH.read_text()).items()}
    score_days = sorted(scores)

    per_symbol_trades: dict[str, list] = {}
    for sym in SPECS:
        bars = _load_bars(sym)
        if not bars:
            print(f"WARN: no data file for {sym} — skipping")
            continue
        trades = []
        for i, d in enumerate(score_days):
            if i < TERCILE_WINDOW:
                continue  # need a trailing window before this day can have a threshold
            window = [scores[dd] for dd in score_days[i - TERCILE_WINDOW:i]]
            lo, hi = np.percentile(window, [33.3, 66.7])
            s = scores[d]
            if s >= hi:
                side = 1
            elif s <= lo:
                side = -1
            else:
                continue
            nxt_candidates = [dd for dd in bars if dd > d]
            if not nxt_candidates:
                continue
            nxt = min(nxt_candidates)
            if (nxt - d).days > 4:  # guard against large gaps (missing data)
                continue
            entry = _bar_at_or_after(bars[nxt], 9 * 60 + 30)
            exit_ = _bar_at_or_before(bars[nxt], 16 * 60)
            if entry is None or exit_ is None:
                continue
            trades.append((nxt, entry, exit_, side))
        per_symbol_trades[sym] = trades

    cost_pts = {sym: SPECS[sym]["comm_rt"] / SPECS[sym]["pt"] + 2 * SLIP_TICKS * SPECS[sym]["tick"]
                for sym in SPECS}
    net_pts, yearly = [], {}
    for sym, trades in per_symbol_trades.items():
        for d, ep, xp, side in trades:
            pnl_pts = (xp - ep) * side - cost_pts[sym]
            net_pts.append(pnl_pts)
            yearly[d.year] = yearly.get(d.year, 0.0) + pnl_pts
    net = np.array(net_pts)
    result: dict = {"n": int(len(net))}
    if len(net) > 2 and net.std(ddof=1) > 0:
        from math import erf, sqrt
        t = float(net.mean() / (net.std(ddof=1) / np.sqrt(len(net))))
        p = 1 - 0.5 * (1 + erf(t / sqrt(2)))
        rng = np.random.default_rng(RNG_SEED)
        bp = float((rng.choice(net, size=(BOOT_N, len(net)), replace=True).mean(axis=1) <= 0).mean())
        gp, gl = net[net > 0].sum(), net[net <= 0].sum()
        pos_years = sum(1 for x in yearly.values() if x > 0)
        result.update({
            "pf": round(float(gp / -gl), 3) if gl < 0 else None,
            "t": round(t, 3), "p_one_sided": round(p, 5), "p_bootstrap": round(bp, 5),
            "win_pct": round(100 * float((net > 0).mean()), 1),
            "yearly_pts": {str(y): round(x, 2) for y, x in sorted(yearly.items())},
            "pct_years_positive": round(100 * pos_years / len(yearly), 1) if yearly else 0.0,
        })
    print(json.dumps(result, indent=1))
    (OUT / "round14_uw_tide_results.json").write_text(json.dumps(result, indent=1))
    print("\nNOTE: check `n` against the PASS-bar TIER that the diagnostic step selected "
          "(HYPOTHESES.md Round 14) before treating any PASS as actionable.")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if "--diagnostic" in args:
        return diagnostic()
    if "--pull" in args:
        since = date(2015, 1, 1)
        if "--since" in args:
            since = date.fromisoformat(args[args.index("--since") + 1])
        return pull(since)
    return backtest()


if __name__ == "__main__":
    raise SystemExit(main())
