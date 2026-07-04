"""Tests for the Unusual Whales aggressor-side fix + signal-quality logger.

Fully offline: _parse is pure (no network), and the logger/analyzer work on a
tmp CSV. No UW_API_KEY required.
"""
from __future__ import annotations

import uw_flow
from uw_flow import UWFlowFeed, _is_bullish
from uw_logger import UWFlowLogger, _forward_pairs, analyze


def _ticket(kind: str, prem: float, side: str = "") -> dict:
    return {"type": kind, "total_premium": prem, "side": side,
            "expiry": "2026-07-18", "strike": 5000}


# ── fix (a): aggressor-side classification ───────────────────────────────────
def test_is_bullish_by_type_and_side():
    assert _is_bullish("CALL", "ASK") is True    # call bought  → bullish
    assert _is_bullish("CALL", "BID") is False   # call sold    → bearish
    assert _is_bullish("PUT", "ASK") is False    # put bought   → bearish
    assert _is_bullish("PUT", "BID") is True      # put sold     → bullish
    assert _is_bullish("CALL", "") is None        # ambiguous    → caller decides
    assert _is_bullish("CALL", "MID") is None


def test_calls_sold_read_bearish_not_bullish():
    # The old formula counted all call premium as bullish; a block of calls SOLD
    # is bearish intent and must now drive the lean negative.
    feed = UWFlowFeed()
    try:
        read = feed._parse([_ticket("CALL", 1_000_000, "BID")], "SPX")
    finally:
        feed.close()
    assert read is not None
    assert read.lean < 0                    # bearish, not +1
    assert read.bear_prem == 1_000_000
    assert read.call_prem == 1_000_000      # gross-by-type unchanged (display)


def test_bullish_intent_mix_calls_bought_puts_sold():
    feed = UWFlowFeed()
    try:
        read = feed._parse(
            [_ticket("CALL", 600_000, "ASK"), _ticket("PUT", 400_000, "BID")], "SPX")
    finally:
        feed.close()
    assert read is not None
    assert read.lean > 0                    # both tickets are bullish intent
    assert read.bull_prem == 1_000_000
    assert read.bear_prem == 0.0


def test_missing_side_falls_back_to_type():
    feed = UWFlowFeed()
    try:
        read = feed._parse([_ticket("CALL", 500_000, "")], "SPX")
    finally:
        feed.close()
    assert read is not None
    assert read.lean > 0                    # no side → call treated as bullish
    assert read.bull_prem == 500_000


def test_parse_empty_returns_none():
    feed = UWFlowFeed()
    try:
        assert feed._parse([], "SPX") is None
        assert feed._parse([_ticket("XYZ", 100, "ASK")], "SPX") is None  # non call/put
    finally:
        feed.close()


# ── fix (c): signal-quality logger ───────────────────────────────────────────
def test_logger_disabled_on_empty_path():
    lg = UWFlowLogger("")
    assert not lg.enabled
    lg.log("ES", 5000.0, 0.5, 0.2)  # must be a harmless no-op
    lg.close()


def test_logger_writes_header_and_rows(tmp_path):
    p = tmp_path / "uw.csv"
    lg = UWFlowLogger(str(p))
    assert lg.enabled
    lg.log("ES", 5000.0, 0.50, 0.20, ts=1000.0)
    lg.log("ES", 5010.0, -0.30, 0.10, ts=1300.0)
    lg.close()
    lines = p.read_text().strip().splitlines()
    assert lines[0] == "ts,symbol,spot,uw_lean,quant_lean"
    assert lines[1].startswith("1000.000,ES,5000")
    assert "+0.5000" in lines[1] and "+0.2000" in lines[1]


def test_forward_pairs_computes_return_at_horizon():
    rows = [
        {"ts": 0.0,   "symbol": "ES", "spot": 100.0, "uw_lean": 0.5, "quant_lean": 0.1},
        {"ts": 300.0, "symbol": "ES", "spot": 101.0, "uw_lean": -0.2, "quant_lean": 0.0},
        {"ts": 600.0, "symbol": "ES", "spot": 102.0, "uw_lean": 0.0, "quant_lean": 0.0},
    ]
    uw, qn, fwd = _forward_pairs(rows, horizon_sec=300.0)
    # row0 → row1 (+1%), row1 → row2 (~+0.99%); row2 has no future match
    assert len(fwd) == 2
    assert uw[0] == 0.5
    assert abs(fwd[0] - 0.01) < 1e-9


def test_analyze_runs_on_written_log(tmp_path, capsys):
    p = tmp_path / "uw.csv"
    lg = UWFlowLogger(str(p))
    # UW lean perfectly predicts a +1% forward move each step
    for i in range(6):
        lg.log("ES", 100.0 + i, 0.8, 0.1, ts=float(i * 300))
    lg.close()
    rc = analyze(str(p), horizon_sec=300.0)
    out = capsys.readouterr().out
    assert rc == 0
    assert "UW signal-quality report" in out
    assert "UW lean" in out and "quant lean" in out
