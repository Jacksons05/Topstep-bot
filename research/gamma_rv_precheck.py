"""
Keystone falsification test for the dealer-gamma trading program.

Question 1 (vol suppression): Do POSITIVE net-GEX days precede LOWER realized
volatility than negative net-GEX days -- and does that survive controlling for
the fact that GEX is mechanically anti-correlated with the current vol level?
(If it does NOT survive the control, the "edge" is just volatility persistence
re-labeled -- the Andersen-Bondarenko critique applied to our keystone.)

Question 2 (reversion vs momentum): Is the lag-1 autocorrelation of daily
returns different across GEX regimes? A1 (momentum) needs negative-gamma days to
show POSITIVE autocorr; A4 (reversion) needs positive-gamma days to show
NON-POSITIVE autocorr. This is the daily-frequency shadow of the intraday
signals -- a cheap first-pass, not the final word.

Data: SqueezeMetrics free DIX/GEX series (date, price, dix, gex) from 2011.
  - `gex` sign convention: > 0 = dealers long gamma (vol-suppressing), matching
    the bot's options.py net_gex convention.
  - We only have daily CLOSE prices, so realized vol = annualized std of
    close-to-close log returns. Intraday RV would be sharper; this is the
    lookahead-safe daily proxy.

Lookahead safety: GEX_t is known at the CLOSE of day t (built from that day's
OI). All targets are STRICTLY FORWARD (day t+1 onward). No leakage.

Dependencies: numpy, pandas, httpx. No scipy (stats implemented inline) so this
does not perturb the production venv.

Run:  .venv\\Scripts\\python.exe research\\gamma_rv_precheck.py
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None

DATA_URL = "https://squeezemetrics.com/monitor/static/DIX.csv"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "squeeze_dix_gex.csv")
TRADING_DAYS = 252
RNG = np.random.default_rng(7)  # fixed seed: reproducible bootstrap


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def load_data(refresh: bool = False) -> pd.DataFrame:
    """Load the DIX/GEX series, caching a local copy so re-runs are offline."""
    if refresh or not os.path.exists(CACHE):
        if httpx is None:
            raise RuntimeError("httpx unavailable and no cache present")
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        r = httpx.get(DATA_URL, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        with open(CACHE, "w", encoding="utf-8", newline="") as fh:
            fh.write(r.text)
    df = pd.read_csv(CACHE, parse_dates=["date"]).sort_values("date").reset_index(drop=True)
    df = df.dropna(subset=["price", "gex"]).reset_index(drop=True)
    return df


# --------------------------------------------------------------------------- #
# features / targets
# --------------------------------------------------------------------------- #
def build_frame(df: pd.DataFrame, fwd: int = 5) -> pd.DataFrame:
    """Add returns, current RV (trailing), and STRICTLY-FORWARD realized vol."""
    px = df["price"].to_numpy(float)
    logret = np.concatenate([[np.nan], np.diff(np.log(px))])
    df = df.copy()
    df["logret"] = logret

    # current (trailing, inclusive of t) realized vol -- the confound control.
    # Fixed 10d trailing window, independent of the forward target horizon, so
    # the control is consistent across `fwd` settings (and defined for fwd=1).
    df["rv_now"] = (
        pd.Series(logret).rolling(10).std().to_numpy() * math.sqrt(TRADING_DAYS)
    )
    # forward realized vol over t+1 .. t+fwd (no lookahead: excludes day t)
    fwd_rv = np.full(len(df), np.nan)
    for i in range(len(df)):
        window = logret[i + 1 : i + 1 + fwd]
        if len(window) == fwd and not np.isnan(window).any():
            fwd_rv[i] = np.std(window, ddof=1) * math.sqrt(TRADING_DAYS)
    df["rv_fwd"] = fwd_rv
    # next-day absolute return (annualized) -- sharper, shorter target
    nxt = np.concatenate([logret[1:], [np.nan]])
    df["absret_next"] = np.abs(nxt) * math.sqrt(TRADING_DAYS)
    df["ret_next"] = nxt

    df["pos_gamma"] = df["gex"] > 0
    return df


# --------------------------------------------------------------------------- #
# inline statistics (no scipy)
# --------------------------------------------------------------------------- #
def welch_t(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Welch's t and two-sided p via a normal approximation on the t-stat."""
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    ma, mb = a.mean(), b.mean()
    va, vb = a.var(ddof=1), b.var(ddof=1)
    se = math.sqrt(va / len(a) + vb / len(b))
    t = (ma - mb) / se if se > 0 else 0.0
    p = 2 * (1 - _norm_cdf(abs(t)))  # large-n: t ~ z
    return t, p


def mann_whitney(a: np.ndarray, b: np.ndarray) -> tuple[float, float]:
    """Mann-Whitney U with a normal approximation; returns (rank-biserial, p).
    Rank-biserial > 0 means group `a` tends LARGER than `b`."""
    a, b = a[~np.isnan(a)], b[~np.isnan(b)]
    n1, n2 = len(a), len(b)
    allv = np.concatenate([a, b])
    ranks = pd.Series(allv).rank().to_numpy()
    r1 = ranks[:n1].sum()
    u1 = r1 - n1 * (n1 + 1) / 2
    mu = n1 * n2 / 2
    sd = math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    z = (u1 - mu) / sd if sd > 0 else 0.0
    p = 2 * (1 - _norm_cdf(abs(z)))
    rank_biserial = 2 * u1 / (n1 * n2) - 1
    return rank_biserial, p


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def ols(y: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Plain OLS beta (X already includes an intercept column)."""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    return beta


def block_bootstrap_pval(y, X, coef_idx, blocks=10, iters=2000):
    """Two-sided p-value for a regression coefficient via moving-block bootstrap
    (handles serial correlation in daily vol, which iid resampling would not)."""
    n = len(y)
    base = ols(y, X)[coef_idx]
    n_blocks = int(math.ceil(n / blocks))
    draws = np.empty(iters)
    starts_pool = np.arange(0, n - blocks + 1)
    for k in range(iters):
        starts = RNG.choice(starts_pool, size=n_blocks, replace=True)
        idx = np.concatenate([np.arange(s, s + blocks) for s in starts])[:n]
        draws[k] = ols(y[idx], X[idx])[coef_idx]
    # center on 0 to test H0: coef == 0
    centered = draws - draws.mean()
    p = np.mean(np.abs(centered) >= abs(base))
    return base, p, draws.std()


def lag1_autocorr(r: np.ndarray) -> float:
    r = r[~np.isnan(r)]
    if len(r) < 5:
        return float("nan")
    return float(np.corrcoef(r[:-1], r[1:])[0, 1])


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def pct(x):
    return f"{100 * x:5.2f}%"


def main():
    fwd = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    if fwd < 2:
        sys.exit("forward window must be >= 2 (a 1-day realized-vol std is "
                 "undefined; use fwd>=2, e.g. 5 or 21)")
    refresh = "--refresh" in sys.argv
    df = build_frame(load_data(refresh=refresh), fwd=fwd)
    d = df.dropna(subset=["rv_now", "rv_fwd"]).reset_index(drop=True)

    print("=" * 74)
    print(" DEALER-GAMMA KEYSTONE PRE-CHECK  (SqueezeMetrics DIX/GEX, daily)")
    print("=" * 74)
    print(f" sample: {d['date'].iloc[0].date()} -> {d['date'].iloc[-1].date()}  "
          f"| N = {len(d)} days | forward RV window = {fwd}d")
    print(f" positive-gamma days: {d['pos_gamma'].mean():.1%}   "
          f"negative-gamma days: {(~d['pos_gamma']).mean():.1%}")

    pos = d[d["pos_gamma"]]
    neg = d[~d["pos_gamma"]]

    # ---- Q1a: univariate forward RV by sign ------------------------------- #
    print("\n[Q1a] FORWARD realized vol by GEX sign (lookahead-safe)")
    print(f"   +gamma  mean fwd RV = {pct(pos['rv_fwd'].mean())}   "
          f"median = {pct(pos['rv_fwd'].median())}")
    print(f"   -gamma  mean fwd RV = {pct(neg['rv_fwd'].mean())}   "
          f"median = {pct(neg['rv_fwd'].median())}")
    t, pt = welch_t(pos["rv_fwd"].to_numpy(), neg["rv_fwd"].to_numpy())
    rb, pm = mann_whitney(pos["rv_fwd"].to_numpy(), neg["rv_fwd"].to_numpy())
    print(f"   Welch t = {t:6.2f} (p={pt:.1e})   "
          f"Mann-Whitney rank-biserial = {rb:+.3f} (p={pm:.1e})")
    print("   -> negative rank-biserial = +gamma days have LOWER fwd RV")

    # ---- Q1b: monotonicity across GEX quintiles --------------------------- #
    print("\n[Q1b] Monotonicity: mean forward RV across GEX quintiles (Q1=most -)")
    d["q"] = pd.qcut(d["gex"], 5, labels=False)
    for q in range(5):
        sub = d[d["q"] == q]
        print(f"   Q{q+1}  GEX~[{sub['gex'].min():.2e},{sub['gex'].max():.2e}]  "
              f"mean fwd RV = {pct(sub['rv_fwd'].mean())}   n={len(sub)}")

    # ---- Q1c: THE decisive test -- control for current vol ---------------- #
    print("\n[Q1c] Does GEX predict fwd RV *beyond* current RV? (the real test)")
    y = d["rv_fwd"].to_numpy()
    rv_now_z = (d["rv_now"] - d["rv_now"].mean()) / d["rv_now"].std()
    gex_z = (d["gex"] - d["gex"].mean()) / d["gex"].std()
    # model A: rv_fwd ~ rv_now                (baseline: vol persistence)
    Xa = np.column_stack([np.ones(len(d)), rv_now_z])
    ra = y - Xa @ ols(y, Xa)
    r2a = 1 - ra.var() / y.var()
    # model B: rv_fwd ~ rv_now + gex          (does GEX add anything?)
    Xb = np.column_stack([np.ones(len(d)), rv_now_z.to_numpy(), gex_z.to_numpy()])
    beta_b = ols(y, Xb)
    rb_ = y - Xb @ beta_b
    r2b = 1 - rb_.var() / y.var()
    coef, pboot, se = block_bootstrap_pval(y, Xb, coef_idx=2)
    print(f"   baseline  R^2 (rv_now only)      = {r2a:.4f}")
    print(f"   +GEX      R^2 (rv_now + gex)      = {r2b:.4f}   "
          f"(delta R^2 = {r2b - r2a:+.4f})")
    print(f"   GEX coef (std units)             = {coef:+.4f}  "
          f"block-bootstrap p = {pboot:.3f}")
    survives = (pboot < 0.05) and (coef < 0) and (r2b - r2a > 0.001)
    print(f"   -> GEX independent negative predictor of fwd RV? "
          f"{'YES' if survives else 'NO / marginal'}")

    # ---- Q2: reversion vs momentum by regime ------------------------------ #
    print("\n[Q2] Lag-1 autocorr of daily returns by regime (A1/A4 shadow test)")
    ac_pos = lag1_autocorr(pos["logret"].to_numpy())
    ac_neg = lag1_autocorr(neg["logret"].to_numpy())
    print(f"   +gamma days: lag-1 autocorr = {ac_pos:+.4f}  "
          f"(A4 wants <= 0 : reversion)")
    print(f"   -gamma days: lag-1 autocorr = {ac_neg:+.4f}  "
          f"(A1 wants  > 0 : momentum)")
    a1_ok = ac_neg > ac_pos  # momentum regime more positive than reversion regime
    print(f"   -> momentum(-gamma) > reversion(+gamma) ordering holds? "
          f"{'YES' if a1_ok else 'NO'}")

    # ---- verdict ---------------------------------------------------------- #
    print("\n" + "=" * 74)
    print(" VERDICT")
    print("=" * 74)
    lower_uni = pos["rv_fwd"].mean() < neg["rv_fwd"].mean()
    print(f"   Q1a  +gamma precedes lower fwd RV (univariate) : {yesno(lower_uni)}")
    print(f"   Q1c  ...survives controlling for current vol    : {yesno(survives)}")
    print(f"   Q2   momentum/reversion ordering by regime      : {yesno(a1_ok)}")
    if lower_uni and survives and a1_ok:
        msg = "KEYSTONE SUPPORTED -- proceed to backtest A1/A2/A4."
    elif lower_uni and a1_ok and not survives:
        msg = ("KEYSTONE PARTIALLY SUPPORTED -- the vol-suppression signal is "
               "largely\n   volatility PERSISTENCE, not independent GEX alpha. "
               "The regime\n   ordering (Q2) still holds, so gamma-SIGN gating of "
               "momentum/reversion\n   may add value even if GEX adds little to a "
               "pure vol forecast.")
    else:
        msg = ("KEYSTONE NOT SUPPORTED at daily frequency -- do NOT invest in the "
               "A1/A4\n   backtest yet. Re-test at INTRADAY frequency (the signals "
               "are intraday)\n   before discarding; daily close-to-close may be "
               "too coarse.")
    print("   " + msg)
    print("=" * 74)


def yesno(b):
    return "YES" if b else "no "


if __name__ == "__main__":
    main()
