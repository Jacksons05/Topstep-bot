"""Backtest framework: realistic-cost bracket simulator + the standard stats
kernel and PASS bar used by every round in HYPOTHESES.md.

The fill rules encode the Round-24 post-mortem (a PF-15 mirage caused by
sloppy fills). They are NOT configurable, because every past round was judged
under them and a "flexible" simulator is how goalposts move:

  1. entry at the NEXT bar's OPEN (decision made on the signal bar's close)
  2. never exit on the entry bar itself
  3. valid-geometry only — entry strictly between stop and target in the
     trade's favour, else the setup is SKIPPED (never booked degenerate)
  4. stop and target both touched inside one bar -> STOP (conservative)
  5. costs = round-turn commission + slippage_ticks per SIDE, always applied
  6. a session-end flatten bar is honoured before any other exit

Reporting includes the metrics the account holder asked for (win rate, PF,
Sharpe/Sortino on the trade stream, max drawdown, expectancy, streaks, holding
time, Monte-Carlo ruin probability) — all computed from the realized trade
series, never assumed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from math import erf, sqrt
from pathlib import Path

import numpy as np

from .datasets import SPECS

BOOT_N = 20_000
RNG_SEED = 7                      # frozen across the whole program


@dataclass
class Trade:
    entry_i: int
    exit_i: int
    entry_px: float
    exit_px: float
    side: int                     # +1 long, -1 short
    reason: str = ""              # stop | target | flatten | eod
    meta: dict = field(default_factory=dict)


def simulate_bracket(bars, idxs, signal_k: int, side: int, stop: float,
                     target: float, *, flatten_minute: int | None = None,
                     max_hold_bars: int | None = None) -> Trade | None:
    """Simulate ONE bracketed trade signalled at bars idxs[signal_k].

    Returns a Trade, or None when the setup is skipped (no next bar, or invalid
    geometry). See the module docstring for the frozen fill rules.
    """
    if signal_k + 1 >= len(idxs):
        return None
    ei = idxs[signal_k + 1]
    entry = float(bars.o[ei])                       # rule 1: next bar OPEN
    if side > 0 and not (stop < entry < target):     # rule 3
        return None
    if side < 0 and not (target < entry < stop):
        return None
    for j in range(signal_k + 2, len(idxs)):         # rule 2: never bar ei
        i = idxs[j]
        if flatten_minute is not None and bars.minute_of_day(i) >= flatten_minute:
            return Trade(ei, i, entry, float(bars.c[i]), side, "flatten")
        if side > 0:
            if bars.l[i] <= stop:                    # rule 4: stop wins ties
                return Trade(ei, i, entry, stop, side, "stop")
            if bars.h[i] >= target:
                return Trade(ei, i, entry, target, side, "target")
        else:
            if bars.h[i] >= stop:
                return Trade(ei, i, entry, stop, side, "stop")
            if bars.l[i] <= target:
                return Trade(ei, i, entry, target, side, "target")
        if max_hold_bars is not None and (j - signal_k - 1) >= max_hold_bars:
            return Trade(ei, i, entry, float(bars.c[i]), side, "timeout")
    last = idxs[-1]
    return Trade(ei, last, entry, float(bars.c[last]), side, "eod")


def net_pnl(trades: list[Trade], sym: str, slippage_ticks: float = 1.0) -> np.ndarray:
    """Realized $ P&L per trade, net of commission + slippage on BOTH sides."""
    s = SPECS[sym]
    cost = s["comm_rt"] + 2.0 * slippage_ticks * s["tick"] * s["pt"]
    return np.array([(t.exit_px - t.entry_px) * t.side * s["pt"] - cost
                     for t in trades], dtype=float)


def _streaks(pnl: np.ndarray) -> tuple[int, int]:
    best = cur_w = worst = cur_l = 0
    for x in pnl:
        if x > 0:
            cur_w += 1
            cur_l = 0
            best = max(best, cur_w)
        elif x < 0:
            cur_l += 1
            cur_w = 0
            worst = max(worst, cur_l)
        else:
            cur_w = cur_l = 0
    return best, worst


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    return float(np.max(peak - equity))


def monte_carlo_ruin(pnl: np.ndarray, *, start_equity: float = 50_000.0,
                     ruin_drawdown: float = 2_000.0, n_paths: int = 10_000,
                     trades_per_path: int | None = None,
                     seed: int = RNG_SEED) -> dict:
    """Bootstrap trade order; report P(trailing drawdown breach) and P(target).

    Models the Topstep trailing rule directly: ruin when equity falls
    `ruin_drawdown` below its running peak (the MLL floor), success when it
    gains +$3,000 first. Order matters, so paths resample the realized trades.
    """
    if len(pnl) == 0:
        return {}
    rng = np.random.default_rng(seed)
    n = trades_per_path or min(len(pnl), 200)
    draws = rng.choice(pnl, size=(n_paths, n), replace=True)
    eq = start_equity + np.cumsum(draws, axis=1)
    peak = np.maximum.accumulate(np.concatenate(
        [np.full((n_paths, 1), start_equity), eq], axis=1), axis=1)[:, 1:]
    breached = (eq <= peak - ruin_drawdown)
    hit_target = (eq >= start_equity + 3_000.0)
    first_breach = np.where(breached.any(axis=1), breached.argmax(axis=1), n + 1)
    first_target = np.where(hit_target.any(axis=1), hit_target.argmax(axis=1), n + 1)
    return {
        "paths": n_paths, "trades_per_path": n,
        "p_ruin": round(float((first_breach < first_target).mean()), 4),
        "p_target_first": round(float((first_target < first_breach).mean()), 4),
        "p_neither": round(float(((first_breach > n) & (first_target > n)).mean()), 4),
    }


def evaluate(trades: list[Trade], bars, sym: str, *,
             slippage_ticks: float = 1.0, monte_carlo: bool = True) -> dict:
    """Full metric set for a trade stream. Empty-safe."""
    pnl = net_pnl(trades, sym, slippage_ticks)
    out: dict = {"n": int(len(pnl)), "slippage_ticks": slippage_ticks}
    if len(pnl) == 0:
        return out
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gp, gl = float(wins.sum()), float(losses.sum())
    sd = float(pnl.std(ddof=1)) if len(pnl) > 1 else 0.0
    downside = pnl[pnl < 0]
    dsd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    equity = np.cumsum(pnl)
    best_w, worst_l = _streaks(pnl)
    hold = [t.exit_i - t.entry_i for t in trades]
    out.update({
        "win_pct": round(100.0 * float((pnl > 0).mean()), 1),
        "total_usd": round(float(pnl.sum()), 2),
        "avg_usd": round(float(pnl.mean()), 2),
        "expectancy_usd": round(float(pnl.mean()), 2),
        "pf": round(gp / -gl, 3) if gl < 0 else None,
        "sharpe_per_trade": round(float(pnl.mean() / sd), 3) if sd > 0 else None,
        "sortino_per_trade": round(float(pnl.mean() / dsd), 3) if dsd > 0 else None,
        "max_drawdown_usd": round(_max_drawdown(equity), 2),
        "max_consec_wins": best_w, "max_consec_losses": worst_l,
        "avg_hold_bars": round(float(np.mean(hold)), 1) if hold else None,
        "exit_mix": {r: sum(1 for t in trades if t.reason == r)
                     for r in sorted({t.reason for t in trades})},
    })
    if len(pnl) > 2 and sd > 0:
        t = float(pnl.mean() / (sd / np.sqrt(len(pnl))))
        out["t"] = round(t, 3)
        out["p_one_sided"] = round(1 - 0.5 * (1 + erf(t / sqrt(2))), 5)
    rng = np.random.default_rng(RNG_SEED)
    out["p_bootstrap"] = round(float(
        (rng.choice(pnl, size=(BOOT_N, len(pnl)), replace=True)
         .mean(axis=1) <= 0).mean()), 5)
    yearly = {}
    for t_, p_ in zip(trades, pnl):
        y = bars.ts[t_.entry_i].year
        yearly[y] = yearly.get(y, 0.0) + float(p_)
    out["yearly_usd"] = {str(y): round(x, 2) for y, x in sorted(yearly.items())}
    out["pct_years_positive"] = round(
        100.0 * sum(1 for x in yearly.values() if x > 0) / max(len(yearly), 1), 1)
    if monte_carlo:
        out["monte_carlo"] = monte_carlo_ruin(pnl)
    return out


def passes(cell: dict, *, min_n: int = 200, min_pf: float = 1.15,
           min_years_pct: float = 60.0) -> bool:
    """The standard entry-edge PASS bar used throughout HYPOTHESES.md."""
    return bool(
        cell.get("n", 0) >= min_n
        and (cell.get("pf") or 0) >= min_pf
        and cell.get("p_one_sided") is not None and cell["p_one_sided"] < 0.05
        and cell.get("p_bootstrap") is not None and cell["p_bootstrap"] < 0.05
        and cell.get("pct_years_positive", 0) >= min_years_pct
    )


def sensitivity(build_trades, bars, sym: str, *,
                slippages=(0.0, 1.0, 2.0)) -> dict:
    """Re-price the SAME trade stream at several slippage assumptions.

    Mandatory for any round that passes: Round 26 cleared the bar at 1 tick and
    collapsed to t=0.91 at 2 ticks. A result that only survives an optimistic
    cost model is not an edge.
    """
    trades = build_trades()
    return {f"slip_{s:g}": evaluate(trades, bars, sym, slippage_ticks=s,
                                    monte_carlo=False)
            for s in slippages}


def regime_split(trades: list[Trade], bars, sym: str, *,
                 split_year: int = 2020, slippage_ticks: float = 1.0) -> dict:
    """Split the stream by era — the check that exposed Round 26's edge as a
    post-2019 artifact."""
    early = [t for t in trades if bars.ts[t.entry_i].year < split_year]
    late = [t for t in trades if bars.ts[t.entry_i].year >= split_year]
    return {
        f"pre_{split_year}": evaluate(early, bars, sym,
                                      slippage_ticks=slippage_ticks, monte_carlo=False),
        f"post_{split_year}": evaluate(late, bars, sym,
                                       slippage_ticks=slippage_ticks, monte_carlo=False),
    }


def report(name: str, cells: dict, out_dir: Path | None = None) -> dict:
    """Print + persist a round result block."""
    payload = {"round": name, "cells": cells}
    if out_dir is not None:
        (out_dir / f"{name}_results.json").write_text(json.dumps(payload, indent=1))
    for label, cell in cells.items():
        if not isinstance(cell, dict) or "n" not in cell:
            continue
        print(f"  {label}: n={cell.get('n')} PF={cell.get('pf')} "
              f"t={cell.get('t')} p={cell.get('p_one_sided')} "
              f"boot={cell.get('p_bootstrap')} yrs+={cell.get('pct_years_positive')}% "
              f"win={cell.get('win_pct')}% total=${cell.get('total_usd')}")
    return payload
