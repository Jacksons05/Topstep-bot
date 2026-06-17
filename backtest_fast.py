"""Fast, multi-regime backtest core (v2 §5).

Same quant model as the engine (signals.quant_signal: SMA cross + RSI + ATR
brackets, no look-ahead — entry at the next bar, stop checked before target),
but built for speed and regime validation:

  • Polars  — lazy aggregation of the trade ledger into per-regime stats.
  • Numba   — the bar-by-bar exit simulation is a compiled @njit kernel.
  • Regimes — every trade is tagged Trending / Mean-Reversion / Consolidation /
              Crisis from its entry-bar structure, so an edge that only exists in
              one regime can't hide inside a blended aggregate.

    python backtest_fast.py                       # whole WATCHLIST, daily, 600 bars
    python backtest_fast.py SPY QQQ --bars 800
    python backtest_fast.py --tf 1Day --hold 20

Numbers reconcile with backtest.py (the reference Python loop) — this is the
accelerated, regime-aware version of the same simulation.
"""
from __future__ import annotations

import sys

import numpy as np

from config import CONFIG
from marketdata import MarketData
from regime import regime_labels, REGIMES

# Numba is optional: fall back to a no-op decorator so the module still runs
# (just interpreted) if the compiler isn't installed.
try:
    from numba import njit
    _NUMBA = True
except Exception:  # noqa: BLE001
    _NUMBA = False

    def njit(*a, **k):  # type: ignore
        if a and callable(a[0]):       # bare @njit
            return a[0]

        def wrap(f):                   # @njit(cache=True)
            return f
        return wrap

_REASON = {0: "time", 1: "stop", 2: "target"}


# ── vectorized indicators (match signals.py exactly) ──────────────────────────

def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _rsi(closes: np.ndarray, period: int) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    d = np.diff(closes)
    gains = np.where(d > 0, d, 0.0)
    losses = np.where(d < 0, -d, 0.0)
    cg = np.cumsum(np.insert(gains, 0, 0.0))
    cl = np.cumsum(np.insert(losses, 0, 0.0))
    for i in range(period, n):              # rsi[i] uses deltas d[i-period:i]
        g = (cg[i] - cg[i - period]) / period
        l = (cl[i] - cl[i - period]) / period
        out[i] = 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)
    return out


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.empty(n)
    tr[0] = highs[0] - lows[0]
    pc = closes[:-1]
    tr[1:] = np.maximum.reduce([
        highs[1:] - lows[1:],
        np.abs(highs[1:] - pc),
        np.abs(lows[1:] - pc),
    ])
    c = np.cumsum(np.insert(tr, 0, 0.0))
    out[period - 1:] = (c[period:] - c[:-period]) / period  # atr[i] = mean(tr[i-period+1:i+1])
    return out


def _quant_arrays(closes, highs, lows):
    """Per-bar (direction, strength, atr) mirroring signals.quant_signal."""
    fast = _sma(closes, CONFIG.sma_fast)
    slow = _sma(closes, CONFIG.sma_slow)
    rsi = _rsi(closes, CONFIG.rsi_period)
    a = _atr(highs, lows, closes, CONFIG.atr_period)

    trend = np.where(slow > 0, (fast - slow) / slow, 0.0)
    lean = np.zeros(len(closes))
    lean += np.where(trend > 0, 0.5, 0.0) + np.where(trend < 0, -0.5, 0.0)
    lean += np.where(rsi <= CONFIG.rsi_oversold, 0.5, 0.0) + \
        np.where(rsi >= CONFIG.rsi_overbought, -0.5, 0.0)
    lean = np.clip(lean, -1.0, 1.0)
    # invalidate bars where any indicator is undefined
    valid = ~(np.isnan(fast) | np.isnan(slow) | np.isnan(rsi))
    lean = np.where(valid, lean, 0.0)
    # match backtest.py's loop start (i = sma_slow + 1): no entries before then
    start = CONFIG.sma_slow + 1
    if start > 0:
        lean[:start] = 0.0
    direction = np.sign(lean).astype(np.int64)        # +1 buy, -1 sell, 0 flat
    strength = np.abs(lean)
    return direction, strength, np.nan_to_num(a), fast, slow


# ── compiled execution loop ───────────────────────────────────────────────────

@njit(cache=True)
def _simulate(closes, highs, lows, direction, strength, atr_arr, blocked,
              atr_stop_mult, stop_loss_pct, atr_tgt_mult, take_profit_pct,
              min_strength, max_hold):
    """Walk the series one position at a time; return parallel trade arrays.

    Mirrors backtest.backtest_symbol: enter at bar i+1's close when bar i's
    quant strength clears the gate, exit on stop (checked first), target, or a
    max_hold time-stop. Returns (entry_idx, exit_idx, entry_px, exit_px, side,
    reason) as fixed-size arrays truncated by the returned count.
    """
    n = len(closes)
    cap = n
    e_idx = np.empty(cap, np.int64)
    x_idx = np.empty(cap, np.int64)
    e_px = np.empty(cap, np.float64)
    x_px = np.empty(cap, np.float64)
    side = np.empty(cap, np.int64)
    reason = np.empty(cap, np.int64)
    k = 0
    i = 0
    while i < n - 1:
        if direction[i] == 0 or strength[i] < min_strength or blocked[i] == 1:
            i += 1
            continue
        s = direction[i]
        entry = closes[i + 1]
        a = atr_arr[i]
        stop_dist = atr_stop_mult * a
        if stop_loss_pct * entry > stop_dist:
            stop_dist = stop_loss_pct * entry
        tgt_dist = take_profit_pct * entry if take_profit_pct > 0.0 else atr_tgt_mult * a
        if s > 0:
            stop = entry - stop_dist
            target = entry + tgt_dist
        else:
            stop = entry + stop_dist
            target = entry - tgt_dist

        exit_px = closes[n - 1]
        rsn = 0           # time
        held = 0
        j = i + 2
        end = i + 2 + max_hold
        if end > n:
            end = n
        while j < end:
            held = j - (i + 1)
            hi = highs[j]
            lo = lows[j]
            if s > 0:
                if lo <= stop:
                    exit_px = stop; rsn = 1; break
                if target > 0 and hi >= target:
                    exit_px = target; rsn = 2; break
            else:
                if hi >= stop:
                    exit_px = stop; rsn = 1; break
                if target > 0 and lo <= target:
                    exit_px = target; rsn = 2; break
            exit_px = closes[j]
            j += 1

        e_idx[k] = i + 1
        x_idx[k] = i + 1 + held
        e_px[k] = entry
        x_px[k] = exit_px
        side[k] = s
        reason[k] = rsn
        k += 1
        i += held + 2
    return e_idx[:k], x_idx[:k], e_px[:k], x_px[:k], side[:k], reason[:k]


# ── run one symbol ────────────────────────────────────────────────────────────

def backtest_symbol(md: MarketData, sym: str, tf: str, bars_n: int, max_hold: int,
                    block: frozenset = frozenset()):
    """Return a list of trade dicts. `block` = regimes whose entries are skipped
    (re-simulated, not filtered after the fact — so freed slots can take the next
    eligible non-blocked signal, exactly as the live regime gate would)."""
    b = md.bars(sym, tf, bars_n)
    closes = np.asarray(b.get("close") or [], dtype=np.float64)
    highs = np.asarray(b.get("high") or [], dtype=np.float64)
    lows = np.asarray(b.get("low") or [], dtype=np.float64)
    if len(closes) < CONFIG.sma_slow + 5 or len(highs) != len(closes) or len(lows) != len(closes):
        return []

    direction, strength, atr_arr, fast, slow = _quant_arrays(closes, highs, lows)
    labels = regime_labels(closes, highs, lows)
    blocked = np.array([1 if labels[i] in block else 0 for i in range(len(closes))],
                       dtype=np.int8)
    e_idx, x_idx, e_px, x_px, side, reason = _simulate(
        closes, highs, lows, direction, strength, atr_arr, blocked,
        float(CONFIG.atr_stop_mult), float(CONFIG.stop_loss_pct),
        float(CONFIG.atr_target_mult), float(CONFIG.take_profit_pct),
        float(CONFIG.confidence_threshold), int(max_hold),
    )
    if len(e_idx) == 0:
        return []
    rets = np.where(side > 0, (x_px - e_px) / e_px, (e_px - x_px) / e_px)
    return [
        {"symbol": sym, "regime": str(labels[e_idx[i] - 1]),  # regime at the signal bar
         "side": "BUY" if side[i] > 0 else "SELL",
         "ret": float(rets[i]), "reason": _REASON[int(reason[i])],
         "bars_held": int(x_idx[i] - e_idx[i])}
        for i in range(len(e_idx))
    ]


# ── reporting (Polars lazy aggregation) ───────────────────────────────────────

def report(trades: list[dict], bars_n: int, tf: str) -> None:
    import polars as pl

    if not trades:
        print("No trades generated.")
        return
    lf = pl.LazyFrame(trades)

    def agg(group_col: str | None):
        base = lf.group_by(group_col) if group_col else lf.select(pl.lit("ALL").alias("k"))
        gb = (lf.group_by(group_col) if group_col
              else lf.with_columns(pl.lit("ALL").alias("k")).group_by("k"))
        return gb.agg(
            pl.len().alias("trades"),
            (pl.col("ret") > 0).mean().alias("win"),
            pl.col("ret").sum().alias("tot"),
            pl.col("ret").mean().alias("avg"),
            pl.col("ret").filter(pl.col("ret") > 0).sum().alias("gp"),
            pl.col("ret").filter(pl.col("ret") <= 0).sum().alias("gl"),
        ).collect()

    def line(name, r):
        pf = (r["gp"] / -r["gl"]) if r["gl"] < 0 else float("inf")
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{name:16}{r['trades']:>7}{r['win']*100:>6.0f}%"
              f"{r['tot']*100:>+9.2f}%{r['avg']*100:>+8.2f}%{pf_s:>7}")

    print(f"\n{'REGIME':16}{'trades':>7}{'win':>7}{'totRet':>9}{'avgRet':>8}{'PF':>7}")
    print("─" * 54)
    by_reg = {row["regime"]: row for row in agg("regime").to_dicts()}
    for reg in REGIMES:
        if reg in by_reg:
            line(reg, by_reg[reg])
        else:
            print(f"{reg:16}{0:>7}{'—':>7}{'—':>9}{'—':>8}{'—':>7}")
    print("─" * 54)
    line("TOTAL", agg(None).to_dicts()[0])

    if tf == "1Day" and bars_n < 504:
        print(f"\n⚠ {bars_n} daily bars < 504 (~2yr). v2 §5 wants ≥2yr in-sample — "
              f"rerun with --bars 600+ before trusting these numbers.")
    print(f"\nEqual-weight sum of per-trade returns. Baseline only — not net of "
          f"fees/slippage. Engine={'numba' if _NUMBA else 'python-fallback'}.")


def main() -> int:
    args = sys.argv[1:]
    tf, bars_n, max_hold = "1Day", 600, 20
    block: frozenset = frozenset()
    syms: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--tf":
            tf = args[i + 1]; i += 2
        elif a == "--bars":
            bars_n = int(args[i + 1]); i += 2
        elif a == "--hold":
            max_hold = int(args[i + 1]); i += 2
        elif a == "--block":
            block = frozenset(x.strip() for x in args[i + 1].split(",") if x.strip()); i += 2
        elif a == "--allow":
            allow = frozenset(x.strip() for x in args[i + 1].split(",") if x.strip())
            block = frozenset(REGIMES) - allow; i += 2     # allowlist → block the rest
        else:
            syms.append(a.upper()); i += 1
    syms = syms or list(CONFIG.watchlist)

    print(f"Fast backtest | tf={tf} bars={bars_n} maxHold={max_hold} | "
          f"block={sorted(block) or 'none'} | "
          f"confThresh={CONFIG.confidence_threshold} atrStop={CONFIG.atr_stop_mult} "
          f"atrTgt={CONFIG.atr_target_mult} | numba={_NUMBA}")
    md = MarketData()
    all_trades: list[dict] = []
    for sym in syms:
        all_trades.extend(backtest_symbol(md, sym, tf, bars_n, max_hold, block))
    md.close()
    report(all_trades, bars_n, tf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
