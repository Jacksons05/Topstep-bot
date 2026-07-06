"""Round 15 — regime-transition confluence, multi-bar hold.

Frozen spec: oos/HYPOTHESES.md, Round 15. Honest proxy for the two
positive-control signals in arXiv 2605.04004 (GMM regime + Markov
transition + volume Z-score confluence, ATR-scaled pullback entry,
multi-bar hold) — built only from primitives already in this repo
(regime.py's vol/trend buckets made causal/rolling, candidates.py's C3
20-bar sigma window convention, the bot's own 2x/3x ATR bracket, Round 8's
passive-fill convention). Not a replication of the paper's own method.
Nothing here was tuned after seeing the data.

Usage:  .venv/bin/python oos/round15_regime_confluence.py
"""
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidates import load, evaluate, passes, mins  # noqa: E402

OUT = Path(__file__).resolve().parent

ROLL_WINDOW = 500
VOL_HI_Q, VOL_LO_Q = 0.90, 0.33
TS_HI_Q, TS_LO_Q = 0.66, 0.33
ZVOL_WINDOW = 20
ZVOL_MIN = 1.0
PULLBACK_ATR_MULT = 0.5
FILL_LOOKAHEAD_BARS = 6      # 30 min at 5-min bars
STOP_ATR_MULT = 2.0
TARGET_ATR_MULT = 3.0
MAX_HOLD = 24                # 2h, same convention as backtest_oos.py
SMA_FAST, SMA_SLOW, ATR_PERIOD = 20, 50, 14
RTH_ENTRY_START, RTH_ENTRY_END = 9 * 60 + 30, 15 * 60      # 09:30-15:00 ET
RTH_FLATTEN = 15 * 60 + 55                                  # 15:55 ET
GLOBEX_START, GLOBEX_END = 18 * 60, 9 * 60 + 30             # overnight session


def _sma(x, n):
    out = np.full(x.shape, np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _atr(h, l, c, period=ATR_PERIOD):
    n = len(c)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    pc = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    cs = np.cumsum(np.insert(tr, 0, 0.0))
    out[period - 1:] = (cs[period:] - cs[:-period]) / period
    return out


def _causal_regime_labels(vol, ts_strength):
    """Bucket each bar from quantile thresholds computed ONLY over the
    trailing ROLL_WINDOW bars strictly before it (shift(1) => window is
    [i-ROLL_WINDOW, i-1]). Same bucket logic as regime.py, but rolling
    instead of global-quantile, so it is causal in a backtest."""
    n = len(vol)
    vol_s, ts_s = pl.Series(vol), pl.Series(ts_strength)

    def roll_q(s, q):
        return s.rolling_quantile(quantile=q, window_size=ROLL_WINDOW,
                                   min_samples=ROLL_WINDOW).shift(1).to_numpy()

    vol_hi, vol_lo = roll_q(vol_s, VOL_HI_Q), roll_q(vol_s, VOL_LO_Q)
    ts_hi, ts_lo = roll_q(ts_s, TS_HI_Q), roll_q(ts_s, TS_LO_Q)

    labels = np.array([None] * n, dtype=object)
    valid = np.isfinite(vol_hi) & np.isfinite(ts_hi) & np.isfinite(vol) & np.isfinite(ts_strength)
    for i in np.nonzero(valid)[0]:
        v, t = vol[i], ts_strength[i]
        if v >= vol_hi[i]:
            labels[i] = "Crisis"
        elif t >= ts_hi[i]:
            labels[i] = "Trending"
        elif v <= vol_lo[i] and t <= ts_lo[i]:
            labels[i] = "Consolidation"
        else:
            labels[i] = "Mean-Reversion"
    return labels


def _zvol(volume):
    v = pl.Series(volume)
    mean = v.rolling_mean(window_size=ZVOL_WINDOW, min_samples=ZVOL_WINDOW).shift(1).to_numpy()
    std = v.rolling_std(window_size=ZVOL_WINDOW, min_samples=ZVOL_WINDOW).shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (volume - mean) / std
    return z


def _in_session(dt, allow_overnight):
    m = mins(dt)
    if allow_overnight:
        return m >= GLOBEX_START or m < GLOBEX_END
    return RTH_ENTRY_START <= m < RTH_ENTRY_END


def run_signal(ts, o, h, l, c, v, allow_overnight=False):
    """Return list of (entry_idx, exit_idx, entry_px, exit_px, side)."""
    n = len(c)
    fast, slow = _sma(c, SMA_FAST), _sma(c, SMA_SLOW)
    atr = _atr(h, l, c)
    vol = np.where(c > 0, atr / c, np.nan)
    ts_strength = np.where(slow != 0, np.abs(fast - slow) / slow, np.nan)
    labels = _causal_regime_labels(vol, ts_strength)
    zvol = _zvol(v)

    trades = []
    i = 1
    end_bound = n - FILL_LOOKAHEAD_BARS - MAX_HOLD - 1
    while i < end_bound:
        if labels[i] is None or labels[i - 1] is None:
            i += 1
            continue
        if labels[i] == labels[i - 1] or labels[i] not in ("Trending", "Crisis"):
            i += 1
            continue
        if not (np.isfinite(zvol[i]) and zvol[i] >= ZVOL_MIN):
            i += 1
            continue
        if not (np.isfinite(fast[i]) and np.isfinite(slow[i]) and slow[i] != 0):
            i += 1
            continue
        if not _in_session(ts[i], allow_overnight):
            i += 1
            continue
        side = 1 if fast[i] > slow[i] else (-1 if fast[i] < slow[i] else 0)
        if side == 0 or not np.isfinite(atr[i]):
            i += 1
            continue

        limit_px = c[i] - side * PULLBACK_ATR_MULT * atr[i]
        fill_i = None
        for k in range(1, FILL_LOOKAHEAD_BARS + 1):
            j = i + k
            if side > 0 and l[j] <= limit_px:
                fill_i = j
                break
            if side < 0 and h[j] >= limit_px:
                fill_i = j
                break
        if fill_i is None:
            i += 1
            continue

        entry, a = limit_px, atr[i]
        stop = entry - side * STOP_ATR_MULT * a
        target = entry + side * TARGET_ATR_MULT * a
        exit_px, exit_i = c[fill_i], fill_i
        end = min(fill_i + 1 + MAX_HOLD, n)
        for j in range(fill_i + 1, end):
            hard_flatten = (not allow_overnight) and mins(ts[j]) >= RTH_FLATTEN
            if side > 0:
                if l[j] <= stop:
                    exit_px, exit_i = stop, j
                    break
                if h[j] >= target:
                    exit_px, exit_i = target, j
                    break
            else:
                if h[j] >= stop:
                    exit_px, exit_i = stop, j
                    break
                if l[j] <= target:
                    exit_px, exit_i = target, j
                    break
            exit_px, exit_i = c[j], j
            if hard_flatten:
                break
        trades.append((fill_i, exit_i, entry, exit_px, side))
        i = exit_i + 1
    return trades


def main():
    results = {}
    for sym in ("ES", "MNQ"):
        ts, o, h, l, c, v = load(sym)
        rth_trades = run_signal(ts, o, h, l, c, v, allow_overnight=False)
        on_trades = run_signal(ts, o, h, l, c, v, allow_overnight=True)
        results[sym] = {
            "RTH_confluence": evaluate(rth_trades, ts, sym),
            "overnight_confluence_exploratory": evaluate(on_trades, ts, sym),
        }
    verdict = "PASS" if passes(results["ES"]["RTH_confluence"]) else "FAIL"
    out = {"judged_on": "ES RTH_confluence @ 1-tick slip", "verdict_H_A": verdict, "cells": results}
    (OUT / "round15_results.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps({"verdict_H_A": verdict}, indent=1))
    for sym, cells in results.items():
        for name, cell in cells.items():
            print(f"{sym:4} {name:32} n={cell.get('n', 0):6} total=${cell.get('total_usd', 0):>12} "
                  f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
                  f"yrs+={cell.get('pct_years_positive')}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
