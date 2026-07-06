"""Overnight-drift runner guards: Topstep venue refusal, contract-cap entry
gate, fail-closed position reads, and safe defaults."""
from __future__ import annotations

import dataclasses

import overnight_drift as od
import projectx_executor as px


def test_refuses_projectx_gateway(monkeypatch):
    monkeypatch.setattr(
        px, "CONFIG",
        dataclasses.replace(px.CONFIG, projectx_username="", projectx_api_key=""))
    b = px.ProjectXBroker()
    reason = od.broker_refused(b)
    assert reason is not None and "flatten rule" in reason


def test_allows_non_projectx_adapter():
    class NonPropBroker:          # a future non-prop adapter — different class
        pass
    assert od.broker_refused(NonPropBroker()) is None


def test_run_exits_on_projectx_even_when_enabled(monkeypatch, capsys):
    monkeypatch.setattr(
        px, "CONFIG",
        dataclasses.replace(px.CONFIG, projectx_username="", projectx_api_key=""))
    monkeypatch.setattr(od, "ENABLED", True)
    od.run()                      # must return immediately, never enter the loop
    out = capsys.readouterr().out
    assert "REFUSING to run" in out


class _Broker:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return self._positions


def test_entry_blocked_at_contract_cap(monkeypatch):
    monkeypatch.setattr(od, "QTY", 1)
    monkeypatch.setattr(od, "MAX_ACCOUNT_CONTRACTS", 5)
    b = _Broker([{"qty": 3}, {"qty": 2}])          # 5 open + 1 new > 5
    assert "exceed" in od.entry_blocked(b)


def test_entry_allowed_below_cap(monkeypatch):
    monkeypatch.setattr(od, "QTY", 1)
    monkeypatch.setattr(od, "MAX_ACCOUNT_CONTRACTS", 5)
    b = _Broker([{"qty": 2}])
    assert od.entry_blocked(b) is None


def test_entry_fails_closed_on_position_read_error(monkeypatch):
    class _Boom:
        def get_positions(self):
            raise RuntimeError("api down")
    assert "fail closed" in od.entry_blocked(_Boom())


def test_dry_run_is_the_default(monkeypatch):
    monkeypatch.delenv("OD_DRY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)  # ignore .env
    import importlib
    mod = importlib.reload(od)
    try:
        assert mod.DRY is True    # unset env → logs, never trades
    finally:
        importlib.reload(od)      # restore module state for other tests
