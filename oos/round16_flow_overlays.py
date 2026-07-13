"""Round 16 -- flow-risk OVERLAYS through the harness (see oos/HYPOTHESES.md).

A10 (vol-target sizing) is a RISK OVERLAY, not an entry edge, so it is judged on
risk-adjusted improvement of a positive-expectancy base stream, net of the extra
turnover it creates -- using the harness's own stat conventions (one-sided t +
20k bootstrap, seed 7, per calendar year, net of the ES 1-tick round-trip cost).

Data status: oos/data/ (ES 5-min) and ES_of_1s.npz are not on disk, so:
  * A10 MECHANISM is tested on daily SPX 2011-2026 (the offline positive-drift
    series available: research/data/squeeze_dix_gex.csv `price`).
  * A8 (BVC-VPIN veto) needs intraday signed flow (ES_of_1s.npz) -> DATA-BLOCKED;
    this script reports that and exits that section cleanly.

numpy + stdlib only (matches oos/candidates.py). Run:
  <py> oos/round16_flow_overlays.py
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SPX_CSV = ROOT / "research" / "data" / "squeeze_dix_gex.csv"
NPZ = ROOT / "oos" / "data" / "ES_of_1s.npz"
RESULTS = Path(__file__).resolve().parent / "round16_results.json"

BOOT_N = 20_000
RNG_SEED = 7
TRADING_DAYS = 252
# ES 1-tick round-trip cost, harness convention: comm_rt + 2*slip*tick*pt.
ES_PT, ES_TICK, ES_COMM = 50.0, 0.25, 4.00
RT_COST = ES_COMM + 2 * 1 * ES_TICK * ES_PT     # $29.00 per contract round-trip
VOL_WIN = 22                                    # trailing realized-vol window (days)
CLIP_VARIANTS = {"symmetric[0.34,2.0]": (0.34, 2.0),
                 "derisk_only[0.34,1.0]": (0.34, 1.0)}


# ---- stats (mirrors backtest_oos.py / candidates.py) --------------------- #
def tstat_p(arr):
    """One-sided t-test p for mean > 0 (normal approx, no scipy)."""
    n = len(arr)
    if n < 3 or arr.std(ddof=1) == 0:
        return None, None
    t = arr.mean() / (arr.std(ddof=1) / math.sqrt(n))
    p = 1 - 0.5 * (1 + math.erf(t / math.sqrt(2)))
    return float(t), float(p)


def sharpe(pnl):
    s = pnl.std(ddof=1)
    return float(pnl.mean() / s * math.sqrt(TRADING_DAYS)) if s > 0 else 0.0


def max_dd(pnl):
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())        # most-negative drawdown in $


def ols_alpha(y, x):
    """Return (alpha, beta) from y = alpha + beta*x."""
    X = np.column_stack([np.ones(len(x)), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return float(beta[0]), float(beta[1])


def alpha_t_p(y, x):
    """Alpha (intercept) of y~x with analytic one-sided t and p."""
    n = len(y)
    X = np.column_stack([np.ones(n), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    rss = float(resid @ resid)
    sigma2 = rss / (n - 2)
    xtx_inv = np.linalg.inv(X.T @ X)
    se_alpha = math.sqrt(sigma2 * xtx_inv[0, 0])
    a = float(beta[0])
    t = a / se_alpha if se_alpha > 0 else 0.0
    p = 1 - 0.5 * (1 + math.erf(t / math.sqrt(2)))
    return a, float(t), float(p)


def alpha_boot_p(y, x):
    """Bootstrap P(alpha <= 0), resampling day-pairs (seed 7)."""
    rng = np.random.default_rng(RNG_SEED)
    n = len(y)
    alphas = np.empty(BOOT_N)
    for k in range(BOOT_N):
        idx = rng.integers(0, n, n)
        alphas[k] = ols_alpha(y[idx], x[idx])[0]
    return float((alphas <= 0).mean())


# ---- A10 vol-managed test ------------------------------------------------- #
def load_spx():
    dates, px = [], []
    with open(SPX_CSV, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                p = float(row["price"])
            except (KeyError, ValueError):
                continue
            if p > 0:
                dates.append(row["date"][:4])   # year string
                px.append(p)
    return np.array(dates), np.array(px, float)


def run_a10():
    years_all, px = load_spx()
    r = np.diff(px) / px[:-1]                    # simple daily returns, len N-1
    yr = years_all[1:]
    prc = px[:-1]                                # price at start of each return day
    n = len(r)

    # causal trailing-22d vol, using returns through t-1 (shift by 1)
    sig = np.full(n, np.nan)
    for i in range(VOL_WIN, n):
        sig[i] = r[i - VOL_WIN:i].std(ddof=1)    # excludes r[i] -> no look-ahead
    valid = ~np.isnan(sig) & (sig > 0)

    r, yr, prc, sig = r[valid], yr[valid], prc[valid], sig[valid]
    n = len(r)
    base_pnl = r * prc * ES_PT                    # $ P&L of 1 long ES-equiv contract

    out = {}
    for name, (lo, hi) in CLIP_VARIANTS.items():
        raw_w = 1.0 / sig
        # Moreira-Muir normalization: scalar c so managed full-sample vol == base
        w = np.clip(raw_w, None, None)
        gross = w * r
        c = r.std(ddof=1) / gross.std(ddof=1)
        w = np.clip(c * raw_w, lo, hi)
        turn = np.abs(np.diff(w, prepend=w[0]))
        man_pnl = w * r * prc * ES_PT - turn * RT_COST

        a, t, p_t = alpha_t_p(man_pnl, base_pnl)
        p_b = alpha_boot_p(man_pnl, base_pnl)
        # per-year: managed annual net >= base annual net
        yrs = sorted(set(yr))
        wins = sum(man_pnl[yr == y].sum() >= base_pnl[yr == y].sum() for y in yrs)
        out[name] = {
            "n": n,
            "sharpe_base": round(sharpe(base_pnl), 3),
            "sharpe_managed": round(sharpe(man_pnl), 3),
            "maxdd_base": round(max_dd(base_pnl), 0),
            "maxdd_managed": round(max_dd(man_pnl), 0),
            "alpha_usd_per_day": round(a, 4),
            "alpha_t": round(t, 3),
            "alpha_p_ttest": round(p_t, 5),
            "alpha_p_bootstrap": round(p_b, 5),
            "pct_years_managed_ge_base": round(100 * wins / len(yrs), 1),
            "mean_turnover_per_day": round(float(turn.mean()), 4),
        }
    return out


# ---- A8 (data-blocked) ---------------------------------------------------- #
def run_a8():
    if not NPZ.exists():
        return {"status": "DATA-BLOCKED",
                "reason": f"{NPZ} not on disk; needs Databento ES MBP-10 -> "
                          "oos/mbp10_features.py to emit ES_of_1s.npz `tvol`."}
    # (Runnable path retained for when the file exists.)
    d = np.load(NPZ)
    tvol = d["tvol"].astype(float)
    return {"status": "present-but-not-implemented-inline",
            "n_seconds": int(len(tvol))}


def verdict_a10(res):
    lines = []
    for name, m in res.items():
        cond1 = m["alpha_usd_per_day"] > 0 and m["alpha_p_ttest"] < 0.05 and m["alpha_p_bootstrap"] < 0.05
        cond2 = m["sharpe_managed"] > m["sharpe_base"] and m["maxdd_managed"] > m["maxdd_base"]
        cond3 = m["pct_years_managed_ge_base"] >= 60
        if cond1 and cond2 and cond3:
            v = "PASS (alpha source)"
        elif cond2 and not cond1:
            v = "PARTIAL -- drawdown/Sharpe control only, NOT an alpha source"
        else:
            v = "FAIL"
        lines.append((name, v, cond1, cond2, cond3))
    return lines


def main():
    print("=" * 74)
    print(" ROUND 16 -- flow-risk overlays through the harness")
    print("=" * 74)
    a10 = run_a10()
    print("\n[A10] Volatility-managed sizing on daily SPX (mechanism proxy)")
    for name, m in a10.items():
        print(f"\n  clip {name}   n={m['n']}")
        print(f"    Sharpe   base={m['sharpe_base']}  managed={m['sharpe_managed']}")
        print(f"    max-DD$  base={m['maxdd_base']:,.0f}  managed={m['maxdd_managed']:,.0f}")
        print(f"    alpha    ${m['alpha_usd_per_day']}/day  t={m['alpha_t']}  "
              f"p_t={m['alpha_p_ttest']}  p_boot={m['alpha_p_bootstrap']}")
        print(f"    years managed>=base: {m['pct_years_managed_ge_base']}%   "
              f"mean turnover/day={m['mean_turnover_per_day']}")

    print("\n  VERDICT (A10, adapted overlay PASS bar):")
    for name, v, c1, c2, c3 in verdict_a10(a10):
        print(f"    {name:24s} -> {v}   [alpha:{c1} risk:{c2} years:{c3}]")

    a8 = run_a8()
    print(f"\n[A8] Toxicity veto: {a8['status']}")
    if a8["status"] == "DATA-BLOCKED":
        print(f"    {a8['reason']}")

    RESULTS.write_text(json.dumps({"A10": a10, "A8": a8}, indent=2))
    print(f"\nwrote {RESULTS.name}")
    print("=" * 74)


if __name__ == "__main__":
    main()
