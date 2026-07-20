"""BarClock (bar-boundary detector) + engine event-driven wake-up wiring.

Deterministic: BarClock takes an injectable `now` so no test sleeps on the
real wall clock, and the feed-wiring tests use a fake broker/clock recorder
rather than a real SignalR connection.
"""
from __future__ import annotations

import threading
import time

from bar_clock import BarClock, parse_timeframe_seconds


# ── parse_timeframe_seconds ───────────────────────────────────────────────
def test_parse_timeframe_seconds_minutes_hours_days():
    assert parse_timeframe_seconds("5Min") == 300
    assert parse_timeframe_seconds("1Min") == 60
    assert parse_timeframe_seconds("1Hour") == 3600
    assert parse_timeframe_seconds("1Day") == 86400
    assert parse_timeframe_seconds("15Sec") == 15


def test_parse_timeframe_seconds_falls_back_on_garbage():
    assert parse_timeframe_seconds("") == 300
    assert parse_timeframe_seconds("garbage") == 300
    assert parse_timeframe_seconds("0Min") == 300     # non-positive -> default
    assert parse_timeframe_seconds("xMin") == 300      # unparsable count -> default
    assert parse_timeframe_seconds("1Week", default=60) == 60


# ── BarClock boundary detection ───────────────────────────────────────────
def test_mark_tick_does_not_fire_before_boundary():
    fired = []
    clock = BarClock(300, lambda: fired.append(1))
    # Force a known boundary so the test doesn't depend on the real clock.
    clock._next_boundary = 1000.0  # noqa: SLF001 - white-box test of internal state
    assert clock.mark_tick(now=500.0) is False
    assert clock.mark_tick(now=999.9) is False
    assert fired == []


def test_mark_tick_fires_exactly_once_on_crossing():
    fired = []
    clock = BarClock(300, lambda: fired.append(1))
    clock._next_boundary = 1000.0  # noqa: SLF001
    assert clock.mark_tick(now=1000.0) is True
    assert fired == [1]
    # re-armed for the NEXT boundary — same instant does not double-fire
    assert clock.mark_tick(now=1000.0) is False
    assert fired == [1]


def test_mark_tick_coalesces_a_multi_boundary_gap_into_one_fire():
    """A feed gap spanning several bars must fire once, not replay a backlog."""
    fired = []
    clock = BarClock(300, lambda: fired.append(1))
    clock._next_boundary = 1000.0  # noqa: SLF001
    # a tick lands 20 minutes (4 boundaries) after the expected close
    late = 1000.0 + 20 * 60
    assert clock.mark_tick(now=late) is True
    assert fired == [1]
    # re-armed to the NEXT boundary strictly after `now`, not a backlog
    # replay of the 4 boundaries that were missed in between.
    remaining = clock.seconds_to_next_boundary(now=late)
    assert 0.0 < remaining <= 300.0


def test_mark_tick_swallows_callback_exceptions():
    def _boom():
        raise RuntimeError("boom")

    clock = BarClock(300, _boom)
    clock._next_boundary = 1000.0  # noqa: SLF001
    assert clock.mark_tick(now=1000.0) is True   # must not raise


def test_seconds_to_next_boundary():
    clock = BarClock(300, lambda: None)
    clock._next_boundary = 1000.0  # noqa: SLF001
    assert clock.seconds_to_next_boundary(now=700.0) == 300.0
    assert clock.seconds_to_next_boundary(now=1100.0) == 0.0   # never negative


# ── ProjectXOrderFlowFeed wiring: ticks pulse the attached clock ─────────
class _MockBroker:
    _mock_mode = True
    token = ""


def test_feed_pulses_bar_clock_on_quote_trade_depth():
    from projectx_marketdata import ProjectXOrderFlowFeed

    f = ProjectXOrderFlowFeed(_MockBroker())
    f.get("ES")
    f._cid_to_sym["CID"] = "ES"  # noqa: SLF001

    class _RecordingClock:
        def __init__(self):
            self.calls = 0

        def mark_tick(self, now=None):
            self.calls += 1

    clock = _RecordingClock()
    f.attach_bar_clock(clock)

    f._on_quote(["CID", {"bestBid": 100.0, "bestAsk": 100.25,
                          "bestBidSize": 5, "bestAskSize": 5}])
    f._on_trade(["CID", {"price": 100.0, "volume": 1}])
    f._on_depth(["CID", {"type": 2, "price": 100.0, "volume": 1}])
    assert clock.calls == 3


def test_feed_with_no_clock_attached_is_a_no_op():
    from projectx_marketdata import ProjectXOrderFlowFeed

    f = ProjectXOrderFlowFeed(_MockBroker())
    f.get("ES")
    f._cid_to_sym["CID"] = "ES"  # noqa: SLF001
    # no attach_bar_clock() call — must not raise
    f._on_quote(["CID", {"bestBid": 100.0, "bestAsk": 100.25,
                          "bestBidSize": 5, "bestAskSize": 5}])


# ── Engine.wait_for_next_cycle: event-driven wake beats the poll timeout ──
def _bare_engine_for_wait(interval_sec: float):
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    e.wake_event = threading.Event()
    e.next_interval = lambda: interval_sec
    return e


def test_wait_for_next_cycle_wakes_immediately_when_event_is_set():
    e = _bare_engine_for_wait(5.0)   # would take 5s on a pure poll
    e.wake_event.set()               # simulates a BarClock firing on a live tick
    start = time.monotonic()
    e.wait_for_next_cycle()
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "a pre-set wake event must not wait out the full poll interval"
    assert not e.wake_event.is_set(), "must clear the event so it doesn't double-fire"


def test_wait_for_next_cycle_wakes_early_when_set_from_another_thread():
    e = _bare_engine_for_wait(5.0)

    def _fire_shortly():
        time.sleep(0.05)
        e.wake_event.set()

    threading.Thread(target=_fire_shortly, daemon=True).start()
    start = time.monotonic()
    e.wait_for_next_cycle()
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, "must wake on the event, not wait out the full poll interval"


def test_wait_for_next_cycle_falls_back_to_full_timeout_with_no_feed():
    """No BarClock ever fires (dead/mock feed) -> degrades to plain polling,
    i.e. the wait honors next_interval() as its ceiling."""
    e = _bare_engine_for_wait(0.15)
    start = time.monotonic()
    e.wait_for_next_cycle()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.14, "with no wake-up the old poll-interval ceiling must still apply"
