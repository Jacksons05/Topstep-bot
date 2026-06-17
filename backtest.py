"""Quant-baseline backtest — measure the edge before trusting it live.

Walk-forward replay of the SAME quant model the engine uses (signals.quant_signal),
with ATR-bracket exits simulated bar-by-bar. No LLM, no look-ahead: at bar i the
signal sees only bars[:i+1], entry is at the next bar's open.

    python backtest.py                      # whole WATCHLIST, daily, 400 bars
    python backtest.py AAPL NVDA --tf 1Day  # specific names
    python backtest.py --bars 600 --hold 15

Reports per-symbol and aggregate: trades, win rate, total return, profit factor.
This is your precise, reproducible baseline — run it, then A/B any LLM filter
against these numbers.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from config import CONFIG
from marketdata import MarketData
from signals import atr, quant_signal


@dataclass
class Trade:
    symbol: str
    side: str
    entry: float
    exit: float
    bars_held: int
    reason: str

    @property
    def ret(self) -> float:
        """Signed return fraction (long: exit/entry-1, short: entry/exit-1)."""
        d = 1.0 if self.side == "BUY" else -1.0
        return d * (self.exit - self.entry) / self.entry


def _exit_hit(side: str, stop: float, target: float, hi: float, lo: float):
    """Did stop/target trigger this bar? Stop checked first (conservative)."""
    if side == "BUY":
        if lo <= stop:
            return stop, "stop"
        if target and hi >= target:
            return target, "target"
    else:  # SELL/short
        if hi >= stop:
            return stop, "stop"
        if target and lo <= target:
            return target, "target"
    return None, None


def backtest_symbol(md: MarketData, sym: str, tf: str, bars_n: int, max_hold: int) -> list[Trade]:
    b = md.bars(sym, tf, bars_n)
    closes, highs, lows = b.get("close") or [], b.get("high") or [], b.get("low") or []
    n = len(closes)
    if n < CONFIG.sma_slow + 5:
        return []

    trades: list[Trade] = []
    i = CONFIG.sma_slow + 1
    while i < n - 1:
        window = {"close": closes[: i + 1], "high": highs[: i + 1], "low": lows[: i + 1]}
        q = quant_signal(window)
        # gate identical to the engine's quant-only path: confidence == strength
        if not q or q.direction == "FLAT" or q.strength < CONFIG.confidence_threshold:
            i += 1
            continue

        side = q.direction
        entry = closes[i + 1]            # next-bar open proxy (no look-ahead)
        a = atr(highs[: i + 1], lows[: i + 1], closes[: i + 1], CONFIG.atr_period) or 0.0
        stop_dist = max(CONFIG.atr_stop_mult * a, CONFIG.stop_loss_pct * entry)
        tgt_dist = (CONFIG.take_profit_pct * entry) if CONFIG.take_profit_pct > 0 \
            else CONFIG.atr_target_mult * a
        if side == "BUY":
            stop, target = entry - stop_dist, entry + tgt_dist
        else:
            stop, target = entry + stop_dist, entry - tgt_dist

        # simulate forward until stop/target/time-stop
        exit_px, reason, held = closes[-1], "time", 0
        for j in range(i + 2, min(i + 2 + max_hold, n)):
            held = j - (i + 1)
            px, why = _exit_hit(side, stop, target, highs[j], lows[j])
            if px is not None:
                exit_px, reason = px, why
                break
            exit_px = closes[j]  # mark; if loop ends -> time-stop at last close
        trades.append(Trade(sym, side, entry, exit_px, held, reason))
        i += held + 2  # jump past the closed trade (one position at a time)
    return trades


def _fmt_pct(x: float) -> str:
    return f"{x*100:+.2f}%"


def report(by_symbol: dict[str, list[Trade]]) -> None:
    allt = [t for ts in by_symbol.values() for t in ts]
    print(f"\n{'SYM':6}{'trades':>7}{'win%':>7}{'totRet':>9}{'avgRet':>9}{'PF':>7}")
    print("─" * 45)

    def row(name: str, ts: list[Trade]) -> None:
        if not ts:
            print(f"{name:6}{0:>7}{'—':>7}{'—':>9}{'—':>9}{'—':>7}")
            return
        wins = [t.ret for t in ts if t.ret > 0]
        losses = [t.ret for t in ts if t.ret <= 0]
        tot = sum(t.ret for t in ts)
        winr = len(wins) / len(ts) * 100
        gp, gl = sum(wins), -sum(losses)
        pf = (gp / gl) if gl > 0 else float("inf")
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{name:6}{len(ts):>7}{winr:>6.0f}%{_fmt_pct(tot):>9}"
              f"{_fmt_pct(tot/len(ts)):>9}{pf_s:>7}")

    for sym, ts in by_symbol.items():
        row(sym, ts)
    print("─" * 45)
    row("TOTAL", allt)
    # naive equity curve note
    if allt:
        tot = sum(t.ret for t in allt)
        print(f"\nSum of per-trade returns: {_fmt_pct(tot)} across {len(allt)} trades "
              f"(equal-weight, no compounding). Baseline only — not net of fees/slippage.")


def main() -> int:
    args = [a for a in sys.argv[1:]]
    tf, bars_n, max_hold = "1Day", 400, 20
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
        else:
            syms.append(a.upper()); i += 1
    syms = syms or list(CONFIG.watchlist)

    print(f"Backtest | tf={tf} bars={bars_n} maxHold={max_hold} | "
          f"confThresh={CONFIG.confidence_threshold} atrStop={CONFIG.atr_stop_mult} "
          f"atrTgt={CONFIG.atr_target_mult}")
    md = MarketData()
    by_symbol = {sym: backtest_symbol(md, sym, tf, bars_n, max_hold) for sym in syms}
    md.close()
    report(by_symbol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
