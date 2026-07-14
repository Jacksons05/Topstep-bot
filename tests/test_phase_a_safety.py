"""Phase A safety-hardening regression tests (audit NO-GO remediation).

Locks in the five plumbing fixes that prevent a WRONG/failed liquidation or a
Topstep rule breach — deliberately engine-light, no network:

  A1  phantom-mark guard   — futures roots never priced off Alpaca equities
  A2  broker-confirmed flatten — only book positions the broker confirms flat
  A3  kill-switch at submit — arming mid-cycle refuses an in-flight order
  A4  micro-cap normalization — cap counted in mini-equivalents at both sites
  (A5 fail-closed cold start lives in test_topstep_fixes.py alongside the other
   day_state cases.)
"""
from __future__ import annotations

import dataclasses

import pytest

from config import CONFIG
from state import Position, State


def _pos(symbol: str, side: str, qty: float, entry: float) -> Position:
    return Position(
        symbol=symbol, asset="future", side=side, qty=qty, entry_price=entry,
        size_usd=entry * qty, stop=0.0, target=0.0, kind="test", thesis="",
        opened_at="2026-06-22T12:00:00+00:00", mode="paper",
    )


class _OFlowMark:
    """Fake order-flow feed — the realistic source of a futures mark (A1)."""
    def __init__(self, px):
        self._px = px

    def get(self, symbol):
        px = self._px
        return type("_OF", (), {"has_data": True, "micro_price": px,
                                "bid": px, "ask": px})()


# ── A1: phantom-mark guard ────────────────────────────────────────────────────

def test_quote_returns_none_for_futures_roots():
    """MarketData.quote() must refuse futures roots (they collide with equity
    tickers: ES=Eversource, GC/CL/SI/NQ/…) BEFORE any network call."""
    from marketdata import MarketData
    md = MarketData()
    try:
        for root in ("ES", "MES", "MNQ", "MCL", "MGC", "M2K", "GC", "CL", "ZB"):
            assert md.quote(root) is None, f"{root} must not resolve to an equity quote"
    finally:
        md.close()


def test_mark_never_returns_equity_price_for_futures():
    """engine._mark() must return None for a futures root when the order-flow
    feed is down, NOT fall back to an Alpaca equity price."""
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    e._oflow = None                        # flow feed unavailable
    # A data stub that WOULD hand back an (equity) price if _mark asked it to.
    e.data = type("D", (), {"quote": lambda self, s: type(
        "Q", (), {"price": 60.0})()})()
    assert e._mark("ES") is None, "futures mark must not come from the equity endpoint"
    assert e._mark("MCL") is None
    # With the flow feed present, the real futures mark flows through.
    e._oflow = _OFlowMark(4999.5)
    assert e._mark("ES") == pytest.approx(4999.5)


# ── A3: kill-switch guard at the submit choke point ───────────────────────────

def test_submit_refused_when_kill_switch_armed(monkeypatch):
    """Arming the kill switch (KILL_SWITCH=1) must make ProjectXBroker.submit()
    refuse the order at the send site — even in mock mode, and even though the
    engine already passed its once-per-cycle check."""
    import projectx_executor as px
    monkeypatch.setattr(px, "CONFIG", dataclasses.replace(
        px.CONFIG, projectx_username="", projectx_api_key=""))
    b = px.ProjectXBroker()
    assert b._mock_mode, "test must never talk to the live gateway"

    # Not armed → mock fill as usual.
    assert b.submit("MES", 1, "BUY", 5000.0).status == "filled"

    # Armed mid-flight → refuse (raise), do not silently drop.
    monkeypatch.setenv("KILL_SWITCH", "1")
    with pytest.raises(RuntimeError, match="kill-switch"):
        b.submit("MES", 1, "BUY", 5000.0)


# ── A2: broker-confirmed, idempotent flatten with retry ───────────────────────

class _StubTopstep:
    def record_close(self, pnl, secs):
        pass


class _StubBroker:
    """Minimal ProjectXBroker stand-in for the flatten confirmation path."""
    def __init__(self, still_open_after, flatten_raises=False):
        self._still = still_open_after            # dicts get_positions returns AFTER flatten
        self._flatten_raises = flatten_raises
        self.flatten_calls = 0

    def cancel_order(self, oid):
        return True

    def flatten_all(self):
        self.flatten_calls += 1
        if self._flatten_raises:
            raise RuntimeError("get_positions outage during flatten")
        return {}

    def get_positions(self):
        return list(self._still)


def _flatten_engine(broker, mark=78.0):
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    e._topstep = _StubTopstep()
    e._oflow = _OFlowMark(mark)
    e.state = State()
    e.state.save = lambda: None
    e.executor = type("E", (), {"broker": broker})()
    return e


def _patch_isinstance(monkeypatch, broker):
    """_topstep_flatten_all gates on isinstance(broker, ProjectXBroker); make the
    stub pass that gate without a real broker."""
    import projectx_executor as px
    monkeypatch.setattr(px, "ProjectXBroker", broker.__class__)


def test_flatten_leaves_broker_unconfirmed_position_open_and_retries(monkeypatch):
    # Broker still reports MCL open AFTER the flatten → engine must NOT book it.
    broker = _StubBroker(still_open_after=[{"symbol": "MCL", "contract_id": "CON.F.US.MCLE.Q26"}])
    _patch_isinstance(monkeypatch, broker)
    e = _flatten_engine(broker)
    e.state.add(_pos("MCL", "BUY", 1, 78.0))

    e._topstep_flatten_all("breach")
    assert any(p.symbol == "MCL" for p in e.state.open_positions), \
        "a broker-unconfirmed position must stay in local state to retry"
    assert broker.flatten_calls == 1

    # Next scan retries rather than one-shot latching.
    e._topstep_flatten_all("breach")
    assert broker.flatten_calls == 2
    assert any(p.symbol == "MCL" for p in e.state.open_positions)


def test_flatten_books_only_broker_confirmed_flat(monkeypatch):
    # Broker reports flat AFTER the flatten → engine books the close.
    broker = _StubBroker(still_open_after=[])
    _patch_isinstance(monkeypatch, broker)
    e = _flatten_engine(broker)
    e.state.add(_pos("MCL", "BUY", 1, 78.0))

    e._topstep_flatten_all("EOD flatten")
    assert not any(p.symbol == "MCL" for p in e.state.open_positions), \
        "a broker-confirmed flat position must be booked closed"


def test_flatten_submit_outage_books_nothing(monkeypatch):
    # flatten_all() raises (unknown broker state) → close nothing, retry next scan.
    broker = _StubBroker(still_open_after=[], flatten_raises=True)
    _patch_isinstance(monkeypatch, broker)
    e = _flatten_engine(broker)
    e.state.add(_pos("MCL", "BUY", 1, 78.0))

    e._topstep_flatten_all("breach")
    assert any(p.symbol == "MCL" for p in e.state.open_positions), \
        "a flatten submit outage must not phantom-book a flat"


def test_projectx_flatten_all_returns_map_in_mock_mode(monkeypatch):
    """flatten_all() now returns a dict (per-contract success map), not None."""
    import projectx_executor as px
    monkeypatch.setattr(px, "CONFIG", dataclasses.replace(
        px.CONFIG, projectx_username="", projectx_api_key=""))
    b = px.ProjectXBroker()
    assert b._mock_mode
    assert b.flatten_all() == {}


# ── A4: micro-contract cap in mini-equivalents ────────────────────────────────

def test_mini_equivalents_math():
    from futures_symbols import mini_equivalents, contracts_for_mini_budget
    r = CONFIG.topstep_micro_ratio                       # 10
    assert mini_equivalents("MNQ", 10, r) == pytest.approx(1.0)   # 10 micros = 1 mini
    assert mini_equivalents("MES", 3, r) == pytest.approx(0.3)
    assert mini_equivalents("ES", 2, r) == pytest.approx(2.0)     # a mini counts full
    assert mini_equivalents("WAT", 4, r) == pytest.approx(4.0)    # unknown → full (conservative)
    # inverse: whole contracts that fit a mini-budget
    assert contracts_for_mini_budget("MNQ", 5, r) == 50           # 5 minis = 50 micros
    assert contracts_for_mini_budget("ES", 5, r) == 5
    assert contracts_for_mini_budget("MNQ", 0, r) == 0


def test_contracts_ok_counts_mini_equivalents():
    from topstep_risk import TopstepRiskManager
    ts = TopstepRiskManager(initial_equity=100_000.0)
    limit = CONFIG.topstep_max_contracts                 # 5 minis

    s = State()
    for _ in range(40):                                  # 40 micros = 4.0 mini-equiv
        s.add(_pos("MES", "BUY", 1, 5000.0))
    ok, _ = ts.contracts_ok("MES", s)
    assert ok, "40 MES micros = 4.0 mini-equiv is UNDER the 5-mini cap (was 10x too tight)"

    for _ in range(10):                                  # +10 micros → 5.0 mini-equiv
        s.add(_pos("MES", "BUY", 1, 5000.0))
    ok, _ = ts.contracts_ok("MES", s)
    assert not ok, "50 MES micros = 5.0 mini-equiv is AT the cap → block"


def test_cap_sites_agree_on_a_mix(monkeypatch):
    """The engine qty_cap and contracts_ok speak the same unit: with a mixed
    book at 3.0 mini-equiv open, contracts_ok allows and the qty_cap helper caps
    a new MNQ order at the remaining 2.0 minis = 20 micros."""
    from topstep_risk import TopstepRiskManager
    from futures_symbols import mini_equivalents, contracts_for_mini_budget
    r = CONFIG.topstep_micro_ratio
    ts = TopstepRiskManager(initial_equity=100_000.0)

    s = State()
    for _ in range(30):                                  # 30 MES micros = 3.0 mini-equiv
        s.add(_pos("MES", "BUY", 1, 5000.0))
    ok, _ = ts.contracts_ok("MNQ", s)
    assert ok

    open_mini = sum(mini_equivalents(p.symbol, int(p.qty), r)
                    for p in s.open_positions if not p.shadow)
    remaining = CONFIG.topstep_max_contracts - open_mini
    assert remaining == pytest.approx(2.0)
    assert contracts_for_mini_budget("MNQ", remaining, r) == 20
