"""Round 18 — dealer-hedging conditioning on intraday momentum.

Frozen spec: oos/HYPOTHESES.md, Round 18. Baltussen, Da, Lammers & Martens
(JFE 2021)'s own incremental claim over Gao et al. (2018) / Round 10
(already dead on this exact ES/MNQ data): predictability is concentrated
on high dealer-hedging-demand days, proxied here by elevated realized
volatility of the two most recent FULLY COMPLETED prior sessions (causal,
no lookahead). Trade construction is IDENTICAL to Round 10 — only a
day-level hedging-demand gate is added, no new trade-timing parameters.
Nothing here was tuned after seeing the data.

Usage: .venv/bin/python oos/round18_dealer_hedging_momentum.py
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidates import evaluate, load, mins  # noqa: E402

OUT = Path(__file__).resolve().parent

RTH_OPEN_MIN = 9 * 60 + 30     # 09:30 bar open
R_OPEN_END_MIN = 10 * 60       # 10:00 bar close
ENTRY_MIN = 15 * 60 + 30       # 15:30 bar close
EXIT_MIN = 15 * 60 + 55        # 15:55 bar close (Topstep-legal flatten)
RTH_CLOSE_MIN = 16 * 60        # 16:00 bar close
HEDGE_WINDOW = 60               # trailing sessions for the causal threshold
HEDGE_PCTL = 100.0 * 2 / 3       # 66.7th percentile ("high" hedging demand)


def _daily_bars(ts):
    """Weekday RTH bars grouped by date -> {minute_of_day: bar_index}, in
    chronological day order."""
    by_day: dict = {}
    for i, t in enumerate(ts):
        if t.weekday() >= 5:
            continue
        by_day.setdefault(t.date(), {})[mins(t)] = i
    days = sorted(by_day)
    return [by_day[d] for d in days], days


def run_symbol(sym):
    ts, o, h, l, c, v = load(sym)
    day_bars, days = _daily_bars(ts)
    n = len(days)

    close_1600 = np.full(n, np.nan)
    for i, bars in enumerate(day_bars):
        if RTH_CLOSE_MIN in bars:
            close_1600[i] = c[bars[RTH_CLOSE_MIN]]

    # abs_ret[d] = |close(d-1) - close(d-2)| — fully causal as of day d's open,
    # uses only the two most recent sessions strictly before d.
    abs_ret = np.full(n, np.nan)
    for d in range(2, n):
        a, b = close_1600[d - 1], close_1600[d - 2]
        if np.isfinite(a) and np.isfinite(b):
            abs_ret[d] = abs(a - b)

    trades = []
    for d in range(HEDGE_WINDOW + 2, n):
        if not np.isfinite(abs_ret[d]):
            continue
        window = abs_ret[d - HEDGE_WINDOW: d]
        window = window[np.isfinite(window)]
        # Require most (not all) of the trailing 60 sessions valid — market
        # holidays/missing-bar days scattered through the window are routine
        # and shouldn't zero out the whole window (60-for-60 was a bug: it
        # made almost every window reject on ~4% scattered NaN days).
        if len(window) < int(0.75 * HEDGE_WINDOW):
            continue
        threshold = np.percentile(window, HEDGE_PCTL)
        if abs_ret[d] < threshold:
            continue  # not a high-hedging-demand day — no trade

        bars = day_bars[d]
        if not all(m in bars for m in (RTH_OPEN_MIN, R_OPEN_END_MIN, ENTRY_MIN, EXIT_MIN)):
            continue
        r_open = c[bars[R_OPEN_END_MIN]] - o[bars[RTH_OPEN_MIN]]
        if r_open == 0:
            continue
        side = 1 if r_open > 0 else -1
        ei, xi = bars[ENTRY_MIN], bars[EXIT_MIN]
        trades.append((ei, xi, c[ei], c[xi], side))

    return trades, ts


def passes_round18(cell) -> bool:
    """Frozen Round 18 PASS bar (oos/HYPOTHESES.md) — deliberately stricter
    than the standard candidates.passes() bar since this is a subsample cut
    of an already-mostly-refuted family (Round 10)."""
    return bool(
        cell.get("n", 0) >= 150
        and (cell.get("pf") or 0) >= 1.20
        and cell.get("p_one_sided") is not None and cell["p_one_sided"] < 0.01
        and cell.get("p_bootstrap") is not None and cell["p_bootstrap"] < 0.01
        and (cell.get("pct_years_positive") or 0) >= 65.0
    )


def main() -> int:
    results = {}
    for sym in ("ES", "MNQ"):
        trades, ts = run_symbol(sym)
        results[sym] = evaluate(trades, ts, sym)

    verdict = "PASS" if passes_round18(results["ES"]) else "FAIL"
    out = {
        "judged_on": "ES @ 1-tick slip, high-hedging-demand subsample (Round 18 bar)",
        "verdict": verdict,
        "cells": results,
    }
    (OUT / "round18_results.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({"verdict": verdict}, indent=1))
    for sym, cell in results.items():
        print(f"{sym:4} n={cell.get('n', 0):6} total=${cell.get('total_usd', 0):>12} "
              f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
              f"yrs+={cell.get('pct_years_positive')}%")
    print(f"full results -> {OUT / 'round18_results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
