"""Risk-first safety layers. The doc's defense stack, deterministic.

  - kill switch        : a file on disk halts all new entries instantly
  - daily drawdown     : pause for the day once down DAILY_DRAWDOWN_PCT
  - circuit breakers   : regime move >5% halves size (yellow), >10% halts (red)
  - falling-knife      : per-symbol cooldown blocks rapid re-entries
  - position sizing    : %-of-bankroll cap, scaled by conviction + cb multiplier
  - ATR exits          : stop / target travel with volatility
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from config import CONFIG
from signals import Signal
from state import State

ROOT = Path(__file__).resolve().parent


def kill_switch_active() -> bool:
    return (ROOT / CONFIG.kill_switch_file).exists() or os.getenv("KILL_SWITCH") == "1"


def kelly_fraction(p: float, b: float) -> float:
    """Kelly-optimal capital fraction: f* = (p(b+1) - 1) / b, floored at 0.

    p = win probability, b = payout ratio (avg win / avg loss). Returns 0 when
    there's no positive edge (don't bet) or b <= 0 (no loss sample to size against).
    """
    if b <= 0:
        return 0.0
    f = (p * (b + 1) - 1) / b
    return max(0.0, f)


def _sizing_base(state: State) -> tuple[float, str]:
    """Dollar sizing base before per-signal conviction / circuit-breaker scaling.

    Hard ceiling is always MAX_POSITION_PCT of bankroll. With enough trade
    history, fractional Kelly shrinks the base toward the bot's measured edge;
    a small exploration floor keeps it sampling even when Kelly says ~0.
    """
    cap = CONFIG.max_position_pct * CONFIG.bankroll_usd
    if not CONFIG.kelly_enabled:
        return cap, "pct-cap"

    p, b, n = state.trade_stats()
    if n < CONFIG.kelly_min_trades:
        return cap, f"pct-cap (kelly warmup {n}/{CONFIG.kelly_min_trades})"

    f_full = kelly_fraction(p, b)
    f_used = CONFIG.kelly_fraction * f_full            # ¼-Kelly
    floor = CONFIG.kelly_min_fraction * CONFIG.bankroll_usd
    kelly_usd = max(floor, f_used * CONFIG.bankroll_usd)
    base = min(cap, kelly_usd)
    return base, f"kelly f*={f_full:.3f} p={p:.2f} b={b:.2f} n={n}"


def circuit_breaker(regime_move_pct: float) -> tuple[str, float]:
    """Map the regime symbol's intraday move to (level, size_multiplier)."""
    mv = abs(regime_move_pct) / 100.0
    if mv >= CONFIG.cb_red_pct:
        return "red", 0.0
    if mv >= CONFIG.cb_yellow_pct:
        return "yellow", 0.5
    return "green", 1.0


def check(
    sig: Signal,
    state: State,
    *,
    cb_mult: float = 1.0,
    cooldowns: dict[str, float] | None = None,
) -> tuple[bool, float, str]:
    """Pre-trade gate. Returns (ok, size_usd, reason)."""
    if kill_switch_active():
        return False, 0.0, "KILL_SWITCH active"

    dd_limit = -CONFIG.daily_drawdown_pct * CONFIG.bankroll_usd
    if state.daily_pnl() <= dd_limit:
        return False, 0.0, f"daily drawdown hit ({state.daily_pnl():.0f} <= {dd_limit:.0f})"

    if cb_mult <= 0:
        return False, 0.0, "circuit breaker RED — entries halted"

    if len(state.open_positions) >= CONFIG.max_concurrent:
        return False, 0.0, f"max concurrent ({CONFIG.max_concurrent})"

    if state.has_open(sig.symbol):
        return False, 0.0, "already holding this symbol"

    # falling-knife cooldown
    if cooldowns and sig.symbol in cooldowns:
        elapsed = time.time() - cooldowns[sig.symbol]
        if elapsed < CONFIG.trade_cooldown_sec:
            return False, 0.0, f"cooldown ({int(CONFIG.trade_cooldown_sec - elapsed)}s left)"

    if not sig.meets_min_confidence:
        return False, 0.0, f"below min confidence ({sig.confidence_label})"

    # sizing: fractional-Kelly base (capped at %-of-bankroll), then scaled by
    # per-signal conviction and the circuit-breaker multiplier
    base, basis = _sizing_base(state)
    size = base * max(0.0, min(1.0, sig.confidence)) * cb_mult
    if size < CONFIG.min_executable_size_usd:
        return False, 0.0, f"size {size:.0f} below min {CONFIG.min_executable_size_usd:.0f} [{basis}]"

    return True, round(size, 2), f"ok [{basis}]"


def should_exit(pos, current_price: float) -> str | None:
    """ATR-bracket exit: stop-loss or take-profit. None = hold."""
    if pos.entry_price <= 0:
        return None
    long = pos.side == "BUY"
    if pos.stop:
        if long and current_price <= pos.stop:
            return "stop-loss"
        if not long and current_price >= pos.stop:
            return "stop-loss"
    if pos.target:
        if long and current_price >= pos.target:
            return "take-profit"
        if not long and current_price <= pos.target:
            return "take-profit"
    return None
