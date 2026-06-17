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


def notify(msg: str) -> None:
    log(msg)
    if CONFIG.discord_webhook:
        try:
            httpx.post(CONFIG.discord_webhook, json={"content": msg}, timeout=10)
        except httpx.HTTPError as e:
            log(f"  ! discord failed: {e}")
