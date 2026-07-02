"""Smoke tests for the read-only trading_status snapshot."""
from __future__ import annotations

import json

import trading_status
from trading_status import _gather, _render


def test_gather_has_expected_shape():
    s = _gather()
    for k in ("mode", "broker", "positions", "contracts_open", "contracts_cap",
              "realized_pnl", "day_pnl", "mll_floor", "mll_headroom"):
        assert k in s
    # Fresh $50K account book: floor $2k below start, no positions.
    assert s["contracts_cap"] >= 1
    assert s["mll_headroom"] == s["equity_proxy"] - s["mll_floor"]


def test_render_is_stringable():
    out = _render(_gather())
    assert "trading status" in out
    assert "Topstep risk" in out


def test_main_json_stdout_is_clean(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["trading_status.py", "--json"])
    rc = trading_status.main()
    assert rc == 0
    captured = capsys.readouterr()
    # stdout must be valid JSON (advisories routed to stderr).
    payload = json.loads(captured.out)
    assert payload["mode"] in ("paper", "live")
