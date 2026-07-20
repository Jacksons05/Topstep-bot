"""Round 28 EXPLORATORY PILOT -- real (non-synthetic) SPY/QQQ data via
yfinance. NOT a substitute for the registered Round 28 verdict.

See oos/HYPOTHESES.md "Round 28" for the frozen spec, which is judged on
the actual owned ES/NQ Databento data (2010-2019 overlap, oos/data/*.csv --
does not exist in this cloud sandbox, gitignored/local-machine only) via
oos/round28_relative_value.py. That is the real test; this is not it.

This pilot exists only because this cloud environment CAN reach Yahoo
Finance (free, no key) but CANNOT reach the registered data source. It
answers one narrow question -- "is this mechanism obviously dead on any
real data, or worth the trouble of a real run?" -- using SPY/QQQ (the same
ES->SPY / NQ->QQQ proxy mapping config.py already uses elsewhere for
index-underlying data) over whatever window Yahoo's free 5-min-interval
API allows (~60 trading days). That is FAR below the n>=200 PASS-bar floor
and uses ETFs, not the actual futures, with no commission/slippage model --
this can only ever say "not obviously broken" or "obviously broken", never
PASS or FAIL. Do not report this run's numbers as a verdict.

Usage: .venv/bin/python oos/round28_pilot_spy_qqq.py
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import yfinance as yf

HERE = Path(__file__).resolve().parent
LOOKBACK, Z_ENTRY, Z_EXIT, MAX_HOLD = 30, 2.0, 0.5, 12
ENTRY_FIRST_MIN, ENTRY_LAST_MIN = 10 * 60 + 30, 15 * 60
FLATTEN_MIN = 15 * 60 + 55


def _mins(ts) -> int:
    return ts.hour * 60 + ts.minute


def _fetch(sym: str):
    df = yf.download(sym, period="60d", interval="5m", progress=False)
    df.columns = [c[0] for c in df.columns]  # flatten yfinance's MultiIndex
    return df


def _by_session(df):
    out: dict = defaultdict(list)
    for ts, row in df.iterrows():
        if ts.weekday() >= 5:
            continue
        out[ts.date()].append((ts, row["Open"], row["Close"]))
    return out


def run(sym_a: str, sym_b: str):
    """Mirrors round28_relative_value.py's signal logic exactly (session-
    local divergence z-score, same LOOKBACK/Z_ENTRY/Z_EXIT/MAX_HOLD), fed
    real intraday bars instead of the registered CSVs."""
    df_a, df_b = _fetch(sym_a), _fetch(sym_b)
    sa, sb = _by_session(df_a), _by_session(df_b)
    days = sorted(set(sa) & set(sb))
    trades = []
    for d in days:
        ta = {t: (o, c) for t, o, c in sa[d]}
        tb = {t: (o, c) for t, o, c in sb[d]}
        common = sorted(set(ta) & set(tb))
        if len(common) < LOOKBACK + 2:
            continue
        open_a, open_b = ta[common[0]][0], tb[common[0]][0]
        div_hist, pos = [], None
        for k, t in enumerate(common):
            m = _mins(t)
            close_a, close_b = ta[t][1], tb[t][1]
            div = 100 * (close_b / open_b - 1) - 100 * (close_a / open_a - 1)
            div_hist.append(div)
            if pos is not None:
                side, ea, eb, k_in = pos
                held = k - k_in
                window = np.array(div_hist[-LOOKBACK - 1:-1])
                sd = window.std(ddof=1)
                z = (div - window.mean()) / sd if sd > 0 else 0.0
                if abs(z) <= Z_EXIT or held >= MAX_HOLD or m >= FLATTEN_MIN:
                    trades.append((side, ea, eb, close_a, close_b))
                    pos = None
                continue
            if len(div_hist) < LOOKBACK + 1 or not (ENTRY_FIRST_MIN <= m <= ENTRY_LAST_MIN):
                continue
            window = np.array(div_hist[-LOOKBACK - 1:-1])
            sd = window.std(ddof=1)
            if sd <= 0:
                continue
            z = (div - window.mean()) / sd
            if z >= Z_ENTRY:
                pos = (-1, close_a, close_b, k)
            elif z <= -Z_ENTRY:
                pos = (1, close_a, close_b, k)
        if pos is not None:
            side, ea, eb, k_in = pos
            trades.append((side, ea, eb, ta[common[-1]][1], tb[common[-1]][1]))
    return trades, (str(days[0]) if days else None, str(days[-1]) if days else None)


def _evaluate(net) -> dict:
    arr = np.asarray(net, float)
    n = len(arr)
    if n == 0:
        return {"n": 0}
    sd = arr.std(ddof=1)
    t = float(arr.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0
    p_t = 1 - 0.5 * (1 + math.erf(t / math.sqrt(2)))
    gp, gl = arr[arr > 0].sum(), arr[arr <= 0].sum()
    return {"n": n, "win_pct": round(100 * float((arr > 0).mean()), 1),
            "total_usd_per_share": round(float(arr.sum()), 2),
            "pf": round(float(gp / -gl), 3) if gl < 0 else None,
            "t": round(t, 3), "p_one_sided": round(p_t, 5)}


def main() -> int:
    trades, (start, end) = run("SPY", "QQQ")
    # per-SHARE P&L (ETF proxy, no futures point value, no commission/
    # slippage model) -- pilot signal-quality read only, not a $ result.
    net = [-side * (xa - ea) + side * (xb - eb) for side, ea, eb, xa, xb in trades]
    cell = _evaluate(net)
    print(f"window: {start} .. {end} ({cell.get('n', 0)} trades)")
    print(cell)
    print("\nNOT A VERDICT -- see docstring. Run oos/round28_relative_value.py "
          "on the real ES/NQ data for the actual Round 28 judgment.")
    (HERE / "round28_pilot_results.json").write_text(json.dumps(
        {"note": "EXPLORATORY PILOT, SPY/QQQ proxy, NOT the Round 28 verdict",
         "window": {"start": start, "end": end}, "cell": cell}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
