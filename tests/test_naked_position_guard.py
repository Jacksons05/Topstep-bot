"""Regression tests for the 2026-07-17 naked-MCL incident.

Root cause: a creds-present ProjectXBroker fell back to MOCK MODE when the
gateway went unreachable mid-session, so the emergency flatten became a fake
no-op fill while the real position stayed naked. Two fixes:
  1. A creds-present broker NEVER mocks — it fails closed (_unavailable) and
     every order method RAISES instead of faking success.
  2. The engine re-arms-or-flattens any unprotected open futures position every
     cycle (broker-confirmed), not just once at entry.
"""
from __future__ import annotations

import dataclasses

import pytest


def _cfg_with(monkeypatch, **fields):
    import projectx_executor as px
    monkeypatch.setattr(px, "CONFIG", dataclasses.replace(px.CONFIG, **fields))


# ── Fix 1: no mock fallback when credentials are present ─────────────────────

def _make_unavailable_broker(monkeypatch):
    """Construct a ProjectXBroker with creds present but a failing connect."""
    import projectx_executor as px

    _cfg_with(monkeypatch, projectx_username="u", projectx_api_key="k")

    def boom(self):
        raise RuntimeError("Temporary failure in name resolution")

    # Fail the auth step so __init__ hits the except branch.
    monkeypatch.setattr(px.ProjectXBroker, "_authenticate", boom)
    return px.ProjectXBroker()


def test_creds_present_connect_failure_is_unavailable_not_mock(monkeypatch):
    b = _make_unavailable_broker(monkeypatch)
    assert b._unavailable is True
    assert b._mock_mode is False, "creds-present broker must NEVER become mock"


def test_unavailable_broker_submit_raises_not_fakes(monkeypatch):
    from risk import kill_switch_active  # noqa: F401  (ensure importable)
    b = _make_unavailable_broker(monkeypatch)
    import risk
    monkeypatch.setattr(risk, "kill_switch_active", lambda: False)
    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        b.submit("MCL", 6, "BUY", 80.68)


def test_unavailable_broker_flatten_and_stop_raise(monkeypatch):
    b = _make_unavailable_broker(monkeypatch)
    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        b.flatten_all()
    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        b.place_stop_order("MCL", 6, "SELL", 79.0)
    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        b.cancel_order("123")


def test_blank_creds_still_mock(monkeypatch):
    """A genuinely keyless broker keeps working as mock (dev/paper)."""
    import projectx_executor as px
    _cfg_with(monkeypatch, projectx_username="", projectx_api_key="")
    b = px.ProjectXBroker()
    assert b._mock_mode is True
    assert b._unavailable is False
    import risk
    monkeypatch.setattr(risk, "kill_switch_active", lambda: False)
    fill = b.submit("MCL", 1, "BUY", 80.0)   # mock returns a fake fill, as intended
    assert fill.status == "filled"


# ── Fix 2: engine re-arms-or-flattens an unprotected position every cycle ────

class _Broker:
    def __init__(self, stop_result="stop-1", flatten_raises=False):
        self._stop_result = stop_result
        self._flatten_raises = flatten_raises
        self.flatten_calls = 0
        self.stop_calls = 0

    def place_stop_order(self, symbol, qty, side, stop, mark=None):
        self.stop_calls += 1
        if isinstance(self._stop_result, Exception):
            raise self._stop_result
        return self._stop_result

    def flatten_all(self):
        self.flatten_calls += 1
        if self._flatten_raises:
            raise RuntimeError("gateway down")
        return {}


def _engine_with(pos, broker, monkeypatch):
    import engine as eng
    monkeypatch.setattr(eng, "notify", lambda *a, **k: None)
    e = eng.Engine.__new__(eng.Engine)
    e._live_projectx = lambda: True

    class _Exec:
        pass
    e.executor = _Exec()
    e.executor.broker = broker

    class _State:
        def __init__(self, positions):
            self.open_positions = positions
            self.saved = False

        def save(self):
            self.saved = True
    e.state = _State([pos])
    e._mark = lambda s: 80.0
    return e


def _pos(**kw):
    from state import Position
    d = dict(symbol="MCL", asset="future", side="BUY", qty=6, entry_price=80.68,
             size_usd=484.0, stop=79.0, target=82.0, kind="gex", thesis="t",
             opened_at="2026-07-17T14:46:00+00:00", mode="paper", filled=True,
             protective_order_id="")
    d.update(kw)
    return Position(**d)


def test_unprotected_position_rearms_stop(monkeypatch):
    pos = _pos()
    b = _Broker(stop_result="stop-9")
    e = _engine_with(pos, b, monkeypatch)
    e._enforce_stops_or_flatten()
    assert pos.protective_order_id == "stop-9"
    assert b.flatten_calls == 0  # re-armed, no need to flatten


def test_unprotected_position_flattens_when_rearm_fails(monkeypatch):
    pos = _pos()
    b = _Broker(stop_result=RuntimeError("outside range"))
    e = _engine_with(pos, b, monkeypatch)
    e._enforce_stops_or_flatten()
    assert b.flatten_calls == 1  # couldn't arm → flattened

def test_protected_position_untouched(monkeypatch):
    pos = _pos(protective_order_id="already-resting")
    b = _Broker()
    e = _engine_with(pos, b, monkeypatch)
    e._enforce_stops_or_flatten()
    assert b.stop_calls == 0 and b.flatten_calls == 0


def test_naked_flatten_failure_leaves_position_open_for_retry(monkeypatch):
    """The incident core: flatten fails (outage) → position must stay OPEN
    (not booked closed) so the next cycle retries. Never fake a close."""
    pos = _pos(stop=0.0)  # no stop price → can't re-arm → must flatten
    b = _Broker(flatten_raises=True)
    e = _engine_with(pos, b, monkeypatch)
    e._enforce_stops_or_flatten()   # must NOT raise out
    assert b.flatten_calls == 1
    assert pos.open is True          # still open, will retry next cycle
    assert pos.protective_order_id == ""
