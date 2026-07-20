"""Decision-loop latency fixes (2026-07-20): bar-aligned poll cadence instead
of a flat SCAN_INTERVAL_SEC countdown, a bounded LLM call timeout instead of
an effectively-unbounded one, and an event-driven wake so the loop reacts to
a live order-flow tick immediately instead of waiting out the full interval.
Pure execution-quality changes — no entry logic or risk gating touched."""
from __future__ import annotations

import dataclasses
import threading
import time as _time

import pytest

from config import CONFIG


def _bare_engine():
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    e.cb_level = "green"
    e._wake_event = threading.Event()
    return e


# ── bar-aligned cadence ───────────────────────────────────────────────────
def test_next_interval_uses_closed_interval_when_market_shut(monkeypatch):
    e = _bare_engine()
    monkeypatch.setattr(e, "_market_open", lambda: False)
    assert e.next_interval() == CONFIG.closed_interval_sec


def test_next_interval_stays_flat_fast_poll_when_breaker_tripped(monkeypatch):
    e = _bare_engine()
    e.cb_level = "red"
    monkeypatch.setattr(e, "_market_open", lambda: True)
    # Breaker-tripped cadence is risk-monitoring responsiveness, not signal
    # freshness — must NOT be bar-aligned even if it happens to land near a
    # boundary (verified across a few phases into the 300s bar).
    for phase in (0, 100, 299):
        monkeypatch.setattr("engine.time.time", lambda: 1_000_000 + phase)
        assert e.next_interval() == CONFIG.fast_interval_sec


def test_bar_aligned_interval_wakes_shortly_after_next_boundary(monkeypatch):
    e = _bare_engine()
    monkeypatch.setattr(e, "_market_open", lambda: True)
    period = CONFIG.bar_align_sec
    buf = CONFIG.bar_align_buffer_sec

    # Use an exact multiple of the bar period so the math is unambiguous.
    base = 1_700_000_000.0 - (1_700_000_000.0 % period)  # exact boundary

    monkeypatch.setattr("engine.time.time", lambda: base + 0.0)       # right at a boundary
    assert e.next_interval() == period + buf

    monkeypatch.setattr("engine.time.time", lambda: base + period - 1)  # 1s before next boundary
    assert e.next_interval() == 1 + buf

    monkeypatch.setattr("engine.time.time", lambda: base + (period / 2))  # halfway through the bar
    assert e.next_interval() == (period / 2) + buf


def test_bar_align_disabled_falls_back_to_flat_scan_interval(monkeypatch):
    import engine as eng
    e = _bare_engine()
    monkeypatch.setattr(e, "_market_open", lambda: True)
    # engine.py does `from config import CONFIG`, a separate name binding —
    # patch it on the engine module itself, not on config.CONFIG.
    monkeypatch.setattr(eng, "CONFIG", dataclasses.replace(CONFIG, bar_align_sec=0))
    assert e.next_interval() == CONFIG.scan_interval_sec


# ── bounded LLM timeout ────────────────────────────────────────────────────
def test_llm_timeout_sec_configured_and_bounded():
    # A worst-case-hung LLM backend must not be able to stall a decision cycle
    # for minutes (was 180s hardcoded for the Ollama path, unbounded default
    # for the Anthropic SDK path). Both now route through one config knob.
    assert 0 < CONFIG.llm_timeout_sec <= 30


def test_ollama_client_uses_configured_timeout(monkeypatch):
    import agents

    captured = {}

    class _FakeHTTPXClient:
        def __init__(self, base_url, timeout):
            captured["timeout"] = timeout

    monkeypatch.setattr(agents.httpx, "Client", _FakeHTTPXClient)
    agents.OllamaClient("http://localhost:11434")
    assert captured["timeout"] == CONFIG.llm_timeout_sec


def test_anthropic_client_uses_configured_timeout(monkeypatch):
    import agents

    captured = {}

    class _FakeAnthropicModule:
        class Anthropic:
            def __init__(self, api_key, timeout):
                captured["api_key"] = api_key
                captured["timeout"] = timeout

    # Pin llm_enabled explicitly rather than inheriting whatever the ambient
    # CONFIG currently has (e.g. a local .env with LLM_ENABLED=false would
    # otherwise make _build_client() return None before ever reaching the
    # anthropic branch, regardless of backend/key — observed 2026-07-20).
    patched = dataclasses.replace(
        CONFIG, llm_enabled=True, llm_backend="anthropic",
        anthropic_api_key="test-key")
    monkeypatch.setattr("config.CONFIG", patched)
    monkeypatch.setattr(agents, "CONFIG", patched)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", _FakeAnthropicModule)
    client = agents._build_client()
    assert captured["timeout"] == CONFIG.llm_timeout_sec
    assert client is not None


# ── event-driven wake (Engine.wake_wait) ──────────────────────────────────
def test_wake_wait_times_out_like_plain_sleep_when_never_signaled():
    e = _bare_engine()
    start = _time.monotonic()
    woke_early = e.wake_wait(0.1)
    elapsed = _time.monotonic() - start
    assert woke_early is False
    # Allow some slack for OS timer granularity / scheduling jitter under a
    # loaded test run — the contract being checked is "didn't return near-
    # instantly", not an exact 0.1s guarantee.
    assert elapsed >= 0.08


def test_wake_wait_returns_immediately_when_signaled_from_another_thread():
    e = _bare_engine()
    t = threading.Timer(0.02, e._wake_event.set)
    t.start()
    start = _time.monotonic()
    woke_early = e.wake_wait(5.0)   # would hang the test if this didn't work
    elapsed = _time.monotonic() - start
    t.cancel()
    assert woke_early is True
    assert elapsed < 1.0            # nowhere near the 5s timeout


def test_wake_wait_clears_the_event_so_the_next_wait_is_not_stale():
    e = _bare_engine()
    e._wake_event.set()
    assert e.wake_wait(5.0) is True         # consumes the pre-set event
    assert e.wake_wait(0.05) is False       # must NOT still read as set


# ── SignalR tick → bar-boundary wake signal (projectx_marketdata.py) ──────
class _MockBroker:
    _mock_mode = True
    token = ""

    def contract_id(self, symbol):
        raise AssertionError("should not resolve contracts in mock mode")


def _feed(on_boundary=None):
    from projectx_marketdata import ProjectXOrderFlowFeed
    return ProjectXOrderFlowFeed(_MockBroker(), on_boundary=on_boundary)


def test_on_boundary_none_is_a_safe_noop(monkeypatch):
    f = _feed(on_boundary=None)
    f._maybe_signal_boundary()   # must not raise


def test_boundary_fires_once_per_bucket_not_every_tick(monkeypatch):
    calls = []
    f = _feed(on_boundary=lambda: calls.append(1))
    period = CONFIG.bar_align_sec
    base = 1_700_000_000.0 - (1_700_000_000.0 % period)

    monkeypatch.setattr("projectx_marketdata.time.time", lambda: base + 1)
    f._maybe_signal_boundary()
    f._maybe_signal_boundary()
    f._maybe_signal_boundary()
    assert len(calls) == 1          # debounced within the same bucket

    monkeypatch.setattr("projectx_marketdata.time.time", lambda: base + period + 1)
    f._maybe_signal_boundary()
    assert len(calls) == 2          # new bucket -> fires again


def test_boundary_signal_swallows_callback_exceptions():
    def _boom():
        raise RuntimeError("callback blew up")
    f = _feed(on_boundary=_boom)
    f._maybe_signal_boundary()   # must not propagate


def test_on_quote_and_on_trade_trigger_the_boundary_signal():
    calls = []
    f = _feed(on_boundary=lambda: calls.append(1))
    # Malformed/unresolvable args are fine — the boundary signal must fire
    # before any per-symbol engine lookup, since it only proves the feed is
    # alive and time has moved, not that this specific tick parsed cleanly.
    f._on_quote([])
    f._on_trade([])
    assert len(calls) == 1   # both land in the same wall-clock bucket


def test_oflow_construction_without_on_boundary_is_backward_compatible():
    f = _feed()   # no on_boundary kwarg at all — matches every existing caller
    f._on_quote([])
    f._on_trade([])   # must not raise even though nothing is listening
