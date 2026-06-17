"""Console + file + optional Discord notifications."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from config import CONFIG
from signals import Signal


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    line = f"[{_stamp()}] {msg}"
    print(line)
    try:
        with open(CONFIG.log_file, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def signal_msg(sig: Signal, qty: int, size_usd: float, mode: str) -> str:
    trail = " ".join(f"{k[0].upper()}:{v}" for k, v in sig.agents.items()) or "—"
    return (
        f"{sig.asset.upper()} | {mode} | conf {sig.confidence:.2f} "
        f"({sig.confidence_label}) | {sig.side} {sig.symbol} @ {sig.price:.2f} "
        f"x{qty} ${size_usd:.2f} | {sig.thesis[:80]} | agents {trail}"
    )


def _close_action(side: str) -> str:
    """The order side that flattens an open position."""
    return "SELL" if side == "BUY" else "BUY"


def trade_ticket(pos: "Position", conf: float, conf_label: str) -> str:
    """Copy-paste manual-execution ticket for the Tradesea DOM (signal-only mode)."""
    head = "🟢 LONG" if pos.side == "BUY" else "🔴 SHORT"
    risk = abs(pos.entry_price - pos.stop)
    reward = abs(pos.target - pos.entry_price)
    rr = f"  |  R:R {reward / risk:.1f}" if risk > 0 else ""
    return (
        "📋 TRADE TICKET → execute manually on Tradesea\n"
        f"   {head}  {pos.side} {pos.qty} {pos.symbol} @ ~{pos.entry_price:.2f}\n"
        f"   STOP {pos.stop:.2f}   TARGET {pos.target:.2f}{rr}\n"
        f"   conf {conf:.2f} ({conf_label}) | {pos.thesis[:100]}"
    )


def exit_ticket(pos: "Position", price: float, reason: str) -> str:
    """Copy-paste manual-exit ticket for the Tradesea DOM (signal-only mode)."""
    return (
        "📋 EXIT TICKET → execute manually on Tradesea\n"
        f"   ✖ {_close_action(pos.side)} {pos.qty} {pos.symbol} @ ~{price:.2f} ({reason})\n"
        f"   est pnl ${pos.pnl_usd:.2f}"
    )


def notify(msg: str) -> None:
    log(msg)
    if CONFIG.discord_webhook:
        try:
            httpx.post(CONFIG.discord_webhook, json={"content": msg}, timeout=10)
        except httpx.HTTPError as e:
            log(f"  ! discord failed: {e}")
