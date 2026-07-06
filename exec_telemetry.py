"""Execution-quality telemetry — measure before optimizing.

Every order-path event is appended as one JSON line to exec_telemetry.jsonl
(grep/pandas-friendly, no DB, no schema migration) and folded into in-memory
session counters that the dashboard serves under "exec". The point: slippage,
rejection rate, fill-confirm latency and stop churn become NUMBERS you can
watch drift, instead of anecdotes buried in the notifier feed.

Event kinds (the `event` field):
  order_submitted    symbol side qty ref_price latency_ms
  order_filled       symbol side qty ref_price fill_price slippage_ticks
                     slippage_usd confirm_ms  (slippage signed: + = cost)
  order_rejected     symbol side qty error_code error_message
  order_ambiguous    symbol side qty  (OrderStateUnknown — state unresolved)
  stop_placed        symbol side qty stop_price order_id
  stop_reject        symbol reason
  order_cancelled    order_id ok
  stop_assumed_filled symbol  (position gone at broker; resting stop presumed hit)
  feed_heal          silent_s resubscribed

All record() calls are fire-and-forget: telemetry must NEVER break the money
path, so every failure is swallowed after a log line.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
TELEMETRY_PATH = ROOT / "exec_telemetry.jsonl"


class ExecTelemetry:
    """Append-only event log + rolling session aggregates (thread-safe)."""

    def __init__(self, path: Path = TELEMETRY_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()
        self._slip_ticks: list[float] = []   # signed, + = cost
        self._slip_usd: list[float] = []
        self._submit_ms: list[float] = []
        self._confirm_ms: list[float] = []

    # ── recording ─────────────────────────────────────────────────────────────
    def record(self, event: str, **fields) -> None:
        """Append one event line and fold it into the session aggregates."""
        row = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "event": event, **fields}
        try:
            with self._lock:
                self._counts[event] += 1
                if event == "order_filled":
                    if (st := fields.get("slippage_ticks")) is not None:
                        self._slip_ticks.append(float(st))
                    if (su := fields.get("slippage_usd")) is not None:
                        self._slip_usd.append(float(su))
                    if (cm := fields.get("confirm_ms")) is not None:
                        self._confirm_ms.append(float(cm))
                elif event == "order_submitted":
                    if (lm := fields.get("latency_ms")) is not None:
                        self._submit_ms.append(float(lm))
                with self.path.open("a") as f:
                    f.write(json.dumps(row, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 — telemetry must never break trading
            log.warning(f"[telemetry] record({event}) failed: {exc}")

    # ── session summary (dashboard) ──────────────────────────────────────────
    @staticmethod
    def _stats(xs: list[float]) -> dict | None:
        if not xs:
            return None
        s = sorted(xs)
        return {"n": len(s), "mean": round(sum(s) / len(s), 3),
                "worst": round(s[-1], 3), "best": round(s[0], 3)}

    def summary(self) -> dict:
        """Session-scope aggregates for the dashboard 'exec' block."""
        with self._lock:
            fills = self._counts.get("order_filled", 0)
            rejects = self._counts.get("order_rejected", 0)
            attempts = self._counts.get("order_submitted", 0)
            return {
                "counts": dict(self._counts),
                "reject_rate": round(rejects / attempts, 4) if attempts else None,
                "slippage_ticks": self._stats(self._slip_ticks),
                "slippage_usd": self._stats(self._slip_usd),
                "submit_latency_ms": self._stats(self._submit_ms),
                "fill_confirm_ms": self._stats(self._confirm_ms),
                "fills": fills,
            }


# Module-level singleton — import and fire.
TELEM = ExecTelemetry()


def signed_slippage(side: str, ref_price: float, fill_price: float,
                    tick_size: float, tick_value: float) -> tuple[float, float]:
    """(ticks, usd) of slippage, signed so POSITIVE = cost to us.

    BUY filled above ref = cost; SELL filled below ref = cost."""
    pts = (fill_price - ref_price) if side == "BUY" else (ref_price - fill_price)
    if tick_size <= 0:
        return 0.0, 0.0
    ticks = pts / tick_size
    return round(ticks, 3), round(ticks * tick_value, 2)
