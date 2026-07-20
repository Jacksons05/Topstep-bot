"""Decision-loop latency fixes (2026-07-20): bar-aligned poll cadence instead
of a flat SCAN_INTERVAL_SEC countdown, and a bounded LLM call timeout instead
of an effectively-unbounded one. Pure execution-quality changes — no entry
logic or risk gating touched."""
from __future__ import annotations

import dataclasses

import pytest

from config import CONFIG


def _bare_engine():
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    e.cb_level = "green"
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

    monkeypatch.setattr(
        "config.CONFIG",
        dataclasses.replace(CONFIG, llm_backend="anthropic",
                             anthropic_api_key="test-key"))
    monkeypatch.setattr(agents, "CONFIG",
                         dataclasses.replace(CONFIG, llm_backend="anthropic",
                                              anthropic_api_key="test-key"))
    monkeypatch.setitem(__import__("sys").modules, "anthropic", _FakeAnthropicModule)
    client = agents._build_client()
    assert captured["timeout"] == CONFIG.llm_timeout_sec
    assert client is not None
