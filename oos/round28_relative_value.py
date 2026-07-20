"""Round 28 — ES/NQ intraday relative-value mean reversion.

Implements exactly the registered spec (HYPOTHESES.md "Round 28", frozen
BEFORE this file ran). First RELATIVE-VALUE (two-instrument) mechanism
tested in this program — every prior round bets on one instrument's own
price; this bets on the ES/NQ relationship reverting after a session-local
divergence, fully reset each RTH day to avoid NQ's multi-year secular
outperformance vs ES contaminating the signal.

Zero additional data cost: reuses oos/data/ES_5min.csv (owned) and
oos/data/NQ_5min.csv (owned), judged on their overlap window only.

Usage:  .venv/bin/python oos/round28_relative_value.py
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from candidates import load, mins  # noqa: E402

RTH_OPEN, FLATTEN = 9 * 60 + 30, 15 * 60 + 55
ENTRY_FIRST, ENTRY_LAST = 10 * 60 + 30, 15 * 60
LOOKBACK = 30          # trailing bars for the in-session rolling z-score
Z_ENTRY, Z_EXIT = 2.0, 0.5
MAX_HOLD_BARS = 12     # 60 min at 5-min bars
BOOT_N, RNG_SEED = 20_000, 7

# (commission_rt, tick_value) per leg — CME e-mini / micro standard rates.
LEG_COSTS = {
    "ES": (4.00, 12.50), "NQ": (4.00, 5.00),
    "MES": (1.40, 1.25), "MNQ": (1.40, 0.50),
}


def _rth_sessions(ts_a, ts_b):
    """Bars present in BOTH series, grouped by RTH session date. Returns
    dict[date] -> list[(i_a, i_b)] aligned by exact timestamp match."""
    idx_b = {t: j for j, t in enumerate(ts_b)}
    by_day: dict = defaultdict(list)
    for i, t in enumerate(ts_a):
        if t.weekday() >= 5 or not (RTH_OPEN <= mins(t) <= FLATTEN):
            continue
        j = idx_b.get(t)
        if j is not None:
            by_day[t.date()].append((i, j))
    return by_day


def run_pair(sym_a: str, sym_b: str):
    """sym_a=ES-family (long/short leg 1), sym_b=NQ-family (leg 2)."""
    ts_a, oa, ha, la, ca, va = load(sym_a)
    ts_b, ob, hb, lb, cb, vb = load(sym_b)
    by_day = _rth_sessions(ts_a, ts_b)
    days = sorted(by_day)

    trades = []  # (date, entry_z, side, ea, eb, xa, xb, hold_bars)
    stats = {"days": 0, "signals": 0, "overlap_start": None, "overlap_end": None}
    pos = None  # (side, entry_a_px, entry_b_px, k_entered)

    for d in days:
        pairs = by_day[d]
        if not pairs:
            continue
        stats["days"] += 1
        # session opens = first bar's OPEN price (not close)
        open_a = oa[pairs[0][0]]
        open_b = ob[pairs[0][1]]
        div_hist: list[float] = []
        pos = None
        for k, (i, j) in enumerate(pairs):
            t = ts_a[i]
            pct_a = 100.0 * (ca[i] / open_a - 1.0)
            pct_b = 100.0 * (cb[j] / open_b - 1.0)
            div = pct_b - pct_a
            div_hist.append(div)

            if pos is not None:
                side, ea, eb, k_in = pos
                held = k - k_in
                z = None
                if len(div_hist) >= LOOKBACK + 1:
                    window = np.array(div_hist[-LOOKBACK - 1:-1])
                    sd = window.std(ddof=1)
                    z = (div - window.mean()) / sd if sd > 0 else 0.0
                reverted = z is not None and abs(z) <= Z_EXIT
                if reverted or held >= MAX_HOLD_BARS or mins(t) >= FLATTEN:
                    trades.append((d, side, ea, eb, ca[i], cb[j], held))
                    pos = None
                continue

            if len(div_hist) < LOOKBACK + 1 or not (ENTRY_FIRST <= mins(t) <= ENTRY_LAST):
                continue
            window = np.array(div_hist[-LOOKBACK - 1:-1])
            sd = window.std(ddof=1)
            if sd <= 0:
                continue
            z = (div - window.mean()) / sd
            if z >= Z_ENTRY:
                pos = (-1, ca[i], cb[j], k)   # short NQ-leg, long ES-leg
            elif z <= -Z_ENTRY:
                pos = (1, ca[i], cb[j], k)    # long NQ-leg, short ES-leg
            if pos is not None:
                stats["signals"] += 1

        if pos is not None:  # force-close anything still open at session end
            i, j = pairs[-1]
            side, ea, eb, k_in = pos
            trades.append((d, side, ea, eb, ca[i], cb[j], len(pairs) - 1 - k_in))

    if days:
        stats["overlap_start"], stats["overlap_end"] = str(days[0]), str(days[-1])
    return trades, stats


def _net_usd(trades, sym_a: str, sym_b: str):
    """side>0 => long sym_b leg / short sym_a leg (NQ under-performed -> buy
    it back); side<0 => short sym_b leg / long sym_a leg. Both legs' RT
    commission + 1-tick slippage on both sides are charged."""
    comm_a, tick_a = LEG_COSTS[sym_a]
    comm_b, tick_b = LEG_COSTS[sym_b]
    net, years = [], []
    for d, side, ea, eb, xa, xb, held in trades:
        # sym_a leg is OPPOSITE side of sym_b leg (fading the divergence)
        pnl_a = -side * (xa - ea) * (LEG_MULT[sym_a])
        pnl_b = side * (xb - eb) * (LEG_MULT[sym_b])
        cost = comm_a + comm_b + 2 * tick_a + 2 * tick_b  # entry+exit slip both legs
        net.append(pnl_a + pnl_b - cost)
        years.append(d.year)
    return np.array(net, float), years


LEG_MULT = {"ES": 50.0, "NQ": 20.0, "MES": 5.0, "MNQ": 2.0}


def evaluate(net: np.ndarray, years: list) -> dict:
    n = len(net)
    if n == 0:
        return {"n": 0}
    sd = net.std(ddof=1)
    t = float(net.mean() / (sd / math.sqrt(n))) if sd > 0 else 0.0
    p_t = 1 - 0.5 * (1 + math.erf(t / math.sqrt(2)))
    rng = np.random.default_rng(RNG_SEED)
    means = rng.choice(net, size=(BOOT_N, n), replace=True).mean(axis=1)
    p_boot = float((means <= 0).mean())
    gp, gl = net[net > 0].sum(), net[net <= 0].sum()
    yr: dict = {}
    for y, v in zip(years, net):
        yr[y] = yr.get(y, 0.0) + v
    pos_years = sum(1 for v in yr.values() if v > 0)
    return {
        "n": n, "win_pct": round(100 * float((net > 0).mean()), 1),
        "total_usd": round(float(net.sum()), 2), "avg_usd": round(float(net.mean()), 2),
        "pf": round(float(gp / -gl), 3) if gl < 0 else None,
        "t": round(t, 3), "p_one_sided": round(p_t, 5), "p_bootstrap": round(p_boot, 5),
        "pct_years_positive": round(100 * pos_years / len(yr), 1) if yr else 0.0,
    }


def passes(c: dict) -> bool:
    # NOTE: use `is not None` for the p-values, not `x or default` -- a
    # perfect/extreme result can legitimately compute p_one_sided/p_bootstrap
    # == 0.0, and `0.0 or 1` evaluates to 1 in Python (falsy-zero), which
    # would silently force a FAIL on the strongest possible results. Matches
    # candidates.py's passes() (the original, correct reference kernel);
    # round17_moc_drift.py had this bug too -- fixed alongside this file.
    return bool(c.get("n", 0) >= 200 and (c.get("pf") or 0) >= 1.15
                and c.get("p_one_sided") is not None and c["p_one_sided"] < 0.05
                and c.get("p_bootstrap") is not None and c["p_bootstrap"] < 0.05
                and (c.get("pct_years_positive") or 0) >= 60)


def main() -> int:
    out = {"registered": "Round 28 (HYPOTHESES.md, 2026-07-20)"}
    for label, sym_a, sym_b, key in (
        ("PRIMARY (judged): ES/NQ", "ES", "NQ", "primary"),
        ("EXPLORATORY: MES/MNQ", "MES", "MNQ", "exploratory"),
    ):
        try:
            trades, stats = run_pair(sym_a, sym_b)
        except FileNotFoundError as exc:
            print(f"  {label}: DATA MISSING -- {exc}")
            out[key] = {"status": "DATA-MISSING", "detail": str(exc)}
            continue
        net, years = _net_usd(trades, sym_a, sym_b)
        cell = evaluate(net, years)
        cell["funnel"] = stats
        verdict = "PASS" if passes(cell) else "FAIL"
        out[key] = {"verdict": verdict if key == "primary" else "EXPLORATORY-ONLY", "cell": cell}
        print(f"\n=== {label}: {verdict if key == 'primary' else '(exploratory, not judged)'} ===")
        print(f"  funnel={stats} | n={cell.get('n')} total=${cell.get('total_usd')} "
              f"PF={cell.get('pf')} t={cell.get('t')} p_t={cell.get('p_one_sided')} "
              f"p_boot={cell.get('p_bootstrap')} yrs+={cell.get('pct_years_positive')}% "
              f"win={cell.get('win_pct')}%")
    (HERE / "round28_results.json").write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
