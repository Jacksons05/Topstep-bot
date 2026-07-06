"""Execution-telemetry tests: slippage math, aggregates, fail-safety, and the
order-path hooks in ProjectXBroker.submit."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx
import pytest

import projectx_executor as px
from exec_telemetry import ExecTelemetry, signed_slippage
from projectx_executor import OrderStateUnknown


# ── slippage sign convention: positive = cost ─────────────────────────────────
def test_signed_slippage_buy_above_ref_is_cost():
    ticks, usd = signed_slippage("BUY", 5000.00, 5000.50, 0.25, 0.50)  # MES-ish
    assert ticks == 2.0 and usd == 1.0


def test_signed_slippage_sell_above_ref_is_improvement():
    ticks, usd = signed_slippage("SELL", 5000.00, 5000.50, 0.25, 0.50)
    assert ticks == -2.0 and usd == -1.0


# ── recording + aggregates ────────────────────────────────────────────────────
def test_record_appends_jsonl_and_aggregates(tmp_path):
    t = ExecTelemetry(tmp_path / "t.jsonl")
    t.record("order_submitted", symbol="MNQ", latency_ms=120.0)
    t.record("order_filled", symbol="MNQ", slippage_ticks=1.0,
             slippage_usd=0.5, confirm_ms=400.0)
    t.record("order_filled", symbol="MNQ", slippage_ticks=3.0,
             slippage_usd=1.5, confirm_ms=800.0)
    t.record("order_rejected", symbol="MNQ", error_code=17)

    lines = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    assert [l["event"] for l in lines] == [
        "order_submitted", "order_filled", "order_filled", "order_rejected"]
    assert all("ts" in l for l in lines)

    s = t.summary()
    assert s["fills"] == 2
    assert s["reject_rate"] == 1.0            # 1 reject / 1 submitted
    assert s["slippage_ticks"] == {"n": 2, "mean": 2.0, "worst": 3.0, "best": 1.0}
    assert s["submit_latency_ms"]["n"] == 1


def test_record_never_raises_on_unwritable_path(tmp_path):
    t = ExecTelemetry(tmp_path / "no_such_dir" / "t.jsonl")
    t.record("order_filled", slippage_ticks=1.0)   # must swallow the OSError
    assert t.summary()["counts"]["order_filled"] == 1   # counters still work


# ── submit() hooks ────────────────────────────────────────────────────────────
def _broker(monkeypatch, tmp_path, script):
    monkeypatch.setattr(
        px, "CONFIG",
        dataclasses.replace(px.CONFIG, projectx_username="", projectx_api_key="",
                            watchlist=("MNQ", "MES")))
    b = px.ProjectXBroker()
    assert b._mock_mode
    b._mock_mode = False           # exercise the real code path with scripted I/O
    b.account_id = 1
    telem = ExecTelemetry(tmp_path / "t.jsonl")
    monkeypatch.setattr(px, "TELEM", telem)
    monkeypatch.setattr(px.time, "sleep", lambda s: None)
    monkeypatch.setattr(b, "contract_id", lambda sym: "CID.MNQ")
    monkeypatch.setattr(b, "_post", script)
    return b, telem


def test_submit_records_submitted_and_filled_with_slippage(monkeypatch, tmp_path):
    def script(path, body, **kw):
        return {"success": True, "orderId": 42}
    b, telem = _broker(monkeypatch, tmp_path, script)
    monkeypatch.setattr(b, "_avg_fill_price", lambda oid: 5000.50)

    fill = b.submit("MNQ", 1, "BUY", 5000.00)
    assert fill.price == 5000.50
    c = telem.summary()["counts"]
    assert c["order_submitted"] == 1 and c["order_filled"] == 1
    rows = [json.loads(l) for l in (tmp_path / "t.jsonl").read_text().splitlines()]
    filled = next(r for r in rows if r["event"] == "order_filled")
    assert filled["slippage_ticks"] == 2.0        # MNQ tick 0.25 → 0.50pts = 2 ticks
    assert filled["confirm_ms"] is not None


def test_submit_records_rejection(monkeypatch, tmp_path):
    def script(path, body, **kw):
        return {"success": False, "errorCode": 99, "errorMessage": "margin"}
    b, telem = _broker(monkeypatch, tmp_path, script)
    with pytest.raises(RuntimeError, match="order rejected"):
        b.submit("MNQ", 1, "BUY", 5000.00)
    assert telem.summary()["counts"]["order_rejected"] == 1


def test_submit_records_ambiguous(monkeypatch, tmp_path):
    def script(path, body, **kw):
        raise OrderStateUnknown("read timeout after send")
    b, telem = _broker(monkeypatch, tmp_path, script)
    with pytest.raises(OrderStateUnknown):
        b.submit("MNQ", 1, "BUY", 5000.00)
    assert telem.summary()["counts"]["order_ambiguous"] == 1
