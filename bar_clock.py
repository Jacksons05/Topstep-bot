"""Wall-clock bar-boundary detector for event-driven engine wake-ups.

The engine's quant signal is computed off fixed-length bars (CONFIG.
scalp_timeframe, e.g. "5Min"). Historically the only way the decision loop
found out a bar had closed was the next scheduled poll (run.py's
`time.sleep(interval)`), which adds up to `interval` seconds of pure,
avoidable latency on top of everything else.

The live ProjectX SignalR feed (see projectx_marketdata.py) already streams
quotes/trades/depth continuously while the market is open — so any tick that
arrives after a bar boundary has passed is proof the bar closed *right now*.
BarClock turns that into a one-shot callback: feed it every tick via
`mark_tick()`, and it fires `on_close` exactly once, the first time a tick
lands on or after the boundary, then re-arms for the next one.

This is deliberately dumb: no bar aggregation, no OHLC state, no dependency
on tick timestamps (wall-clock only, so it works identically whether the
tick's own payload timestamp is late/skewed or missing). It only answers one
question — "has wall-clock crossed into a new bar since we last checked?" —
and is wired as a pure latency optimization: the engine loop always keeps its
existing timeout-based wait as a safety net, so a quiet/dead feed just
degrades back to the old polling cadence instead of stalling. See
Engine.wait_for_next_cycle().
"""
from __future__ import annotations

import threading
import time
from typing import Callable

# Alpaca-style timeframe suffix -> seconds per unit (mirrors
# ProjectXBroker._TIMEFRAME_UNIT's suffix parsing in projectx_executor.py,
# but resolves straight to seconds since that's all a wall-clock boundary
# needs).
_TIMEFRAME_UNIT_SEC = {"Sec": 1, "Min": 60, "Hour": 3600, "Day": 86400}


def parse_timeframe_seconds(timeframe: str, default: int = 300) -> int:
    """"5Min" -> 300, "1Hour" -> 3600, "15Sec" -> 15. Unrecognized/empty
    strings fall back to `default` (5-minute bars) rather than raising —
    this only ever feeds a latency optimization, never a trading decision."""
    tf = (timeframe or "").strip()
    for suffix, unit_sec in _TIMEFRAME_UNIT_SEC.items():
        if tf.endswith(suffix):
            n = tf[: -len(suffix)]
            try:
                count = int(n) if n else 1
            except ValueError:
                return default
            if count <= 0:
                return default
            return count * unit_sec
    return default


class BarClock:
    """Fires `on_close` once per wall-clock bar-boundary crossing.

    Thread-safe: `mark_tick()` is expected to be called from the SignalR
    client's callback thread(s) while the main loop only ever reads state
    indirectly through the `on_close` callback (typically `threading.Event.
    set`). No lock is held across `on_close` itself so a slow/misbehaving
    callback can't block the feed thread from processing the next tick.
    """

    def __init__(self, period_sec: int, on_close: Callable[[], None]) -> None:
        self._period = max(1, int(period_sec))
        self._on_close = on_close
        self._lock = threading.Lock()
        self._next_boundary = self._boundary_after(time.time())

    def _boundary_after(self, ts: float) -> float:
        return (int(ts // self._period) + 1) * self._period

    def mark_tick(self, now: float | None = None) -> bool:
        """Call on every live tick (quote/trade/depth push).

        Returns True iff this call observed a boundary crossing and fired
        `on_close`. A feed gap spanning multiple boundaries coalesces into a
        single fire (re-arms straight to the boundary after `now`, not a
        backlog of missed ones) — the engine only needs to know "at least
        one bar closed while we weren't looking", not how many.
        """
        now = time.time() if now is None else now
        with self._lock:
            if now < self._next_boundary:
                return False
            self._next_boundary = self._boundary_after(now)
        try:
            self._on_close()
        except Exception:  # noqa: BLE001 - a bad callback must never kill the feed thread
            pass
        return True

    def seconds_to_next_boundary(self, now: float | None = None) -> float:
        now = time.time() if now is None else now
        with self._lock:
            return max(0.0, self._next_boundary - now)
