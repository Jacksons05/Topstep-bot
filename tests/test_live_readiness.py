"""Focused unit tests for the live-readiness hardening fixes.

Money-path math only, fully offline (no network, no real broker):
  * C2/C3  risk-based futures sizing + reject-on-zero (executor.futures_plan / open)
  * C1     native protective stop submitted with the entry + cancel on exit
  * C-ML1/H-ML3  ML NaN/out-of-range p_up and bad ATR map to None, not max-conf BUY
  * C4/H9  order-flow has_data freshness + stale detection
  * M15    crossed/one-sided quote rejected (no poisoned mark)
  * M14    kill switch flattens all open positions
  * H5     position-exit path runs end-to-end (no NameError on q.price)
"""
from __future__ import annotations

import math
import time

import numpy as np
import pytest

import orderflow
from broker import Fill
from config import CONFIG
from executor import Executor, _futures_risk_budget_usd, futures_plan
from orderflow import OrderFlowEngine
from signals import Signal
from state import Position, State


def _sig(symbol: str, side: str, price: float, atr: float) -> Signal:
    return Signal(symbol=symbol, asset="future", side=side, price=price,
                  confidence=0.8, confidence_label="high", thesis="t", atr=atr)


def _pos(symbol: str, side: str, qty: float, entry: float) -> Position:
    return Position(
        symbol=symbol, asset="future", side=side, qty=qty, entry_price=entry,
        size_usd=entry * qty, stop=0.0, target=0.0, kind="test", thesis="",
        opened_at="2026-06-22T12:00:00+00:00", mode="paper",
    )


# ── C2/C3 risk-based futures sizing ──────────────────────────────────────────
def test_futures_plan_sizes_off_risk_budget():
    # budget $500, ES $50/pt, atr 2 → stop dist = 2*2 = 4pts → $200/contract → 2 lots
    assert _futures_risk_budget_usd() == pytest.approx(500.0)
    p = futures_plan(_sig("ES", "BUY", 5000.0, atr=2.0))
    assert p is not None
    assert p.qty == 2
    assert p.stop_price == pytest.approx(4996.0)      # entry - 4pts
    assert p.risk_usd == pytest.approx(400.0)         # 2 * 4pts * $50
    assert p.risk_usd <= _futures_risk_budget_usd()


def test_futures_plan_short_stop_above_entry():
    p = futures_plan(_sig("ES", "SELL", 5000.0, atr=2.0))
    assert p is not None and p.stop_price > 5000.0    # short → stop above


def test_futures_plan_rejects_when_stop_too_wide():
    # atr 40 → dist 80pts → $4000/contract > $500 budget → floor 0 → REJECT (not 1)
    assert futures_plan(_sig("ES", "BUY", 5000.0, atr=40.0)) is None


def test_futures_plan_rejects_invalid_atr():
    assert futures_plan(_sig("ES", "BUY", 5000.0, atr=float("nan"))) is None
    assert futures_plan(_sig("ES", "BUY", 5000.0, atr=0.0)) is None


def test_futures_plan_clamps_to_max_contracts():
    # tiny atr would size huge → clamp to TOPSTEP_MAX_CONTRACTS
    p = futures_plan(_sig("ES", "BUY", 5000.0, atr=0.01))
    assert p is not None and p.qty == CONFIG.topstep_max_contracts


# ── account-wide contract cap: a new order can't exceed remaining capacity ────
def test_futures_plan_respects_remaining_contract_cap():
    # budget would size 2 ES lots, but only 1 contract of account-wide capacity
    # remains → qty clamped to 1 so the account total can't exceed the limit.
    p = futures_plan(_sig("ES", "BUY", 5000.0, atr=2.0), max_contracts=1)
    assert p is not None and p.qty == 1
    assert p.risk_usd == pytest.approx(200.0)     # 1 * 4pts * $50


def test_futures_plan_rejects_when_no_capacity_left():
    # zero remaining capacity → reject outright (never round up to 1 contract)
    assert futures_plan(_sig("MES", "BUY", 5000.0, atr=1.0), max_contracts=0) is None


def test_open_caps_qty_to_remaining_capacity():
    # MES sizes to the full cap on the budget alone; with only 1 slot left the
    # order (and the native protective stop) must be for exactly 1 contract.
    e = _exec_with(_FakeBroker())
    pos = e.open(_sig("MES", "BUY", 9_999.0, atr=1.0), 9_999.0, State(), max_contracts=1)
    assert pos is not None and pos.qty == 1
    sym, qty, side, stop = e.broker.stops[0]
    assert qty == 1


# ── C1 native protective stop wiring (mock broker) ───────────────────────────
class _FakeBroker:
    def __init__(self) -> None:
        self.stops: list[tuple] = []
        self.canceled: list[str] = []
        self.market_orders: list[tuple] = []

    def submit(self, symbol, qty, side, price) -> Fill:
        self.market_orders.append((symbol, qty, side, price))
        return Fill(symbol=symbol, qty=float(qty), side=side, price=price,
                    order_id="entry-1", status="filled")

    def place_stop_order(self, symbol, qty, side, stop_price) -> str:
        self.stops.append((symbol, qty, side, stop_price))
        return "stop-1"

    def cancel_order(self, oid) -> bool:
        self.canceled.append(oid)
        return True


def _exec_with(broker) -> Executor:
    e = Executor.__new__(Executor)        # skip build_broker() / network
    e.broker = broker
    e.mode = "paper"
    return e


def test_open_submits_native_protective_stop_opposite_side():
    e = _exec_with(_FakeBroker())
    st = State()
    pos = e.open(_sig("ES", "BUY", 5000.0, atr=2.0), 9_999.0, st)
    assert pos is not None and pos.qty == 2
    assert pos.protective_order_id == "stop-1"
    sym, qty, side, stop = e.broker.stops[0]
    assert side == "SELL" and qty == 2          # protective stop opposite the long
    assert stop < pos.entry_price               # stop below entry for a long


def test_open_rejects_and_places_no_order_when_unsizable():
    e = _exec_with(_FakeBroker())
    pos = e.open(_sig("ES", "BUY", 5000.0, atr=40.0), 9_999.0, State())
    assert pos is None
    assert e.broker.market_orders == []         # no entry order placed on reject
    assert e.broker.stops == []


def test_close_cancels_resting_protective_stop():
    e = _exec_with(_FakeBroker())
    st = State()
    pos = e.open(_sig("ES", "BUY", 5000.0, atr=2.0), 9_999.0, st)
    e.close(pos, 5005.0, st)
    assert "stop-1" in e.broker.canceled         # resting stop canceled before flatten
    assert not pos.open


# ── C-ML1 / H-ML3 ML probability + ATR validation ────────────────────────────
def _ramp(n: int = 120) -> dict:
    rng = np.random.default_rng(0)
    close = np.cumsum(rng.normal(0.1, 1.0, n)) + 100.0
    high = close + np.abs(rng.normal(0.5, 0.2, n))
    low = close - np.abs(rng.normal(0.5, 0.2, n))
    return {"close": close.tolist(), "high": high.tolist(), "low": low.tolist()}


class _FakeBooster:
    def __init__(self, value: float) -> None:
        self._v = value

    def predict(self, vec):
        return [self._v]


def _ml_with(value: float):
    from ml_signal import MLQuant
    m = MLQuant.__new__(MLQuant)
    m._booster = _FakeBooster(value)
    m._loaded = True
    m._load_failed = False
    return m


def test_ml_nan_probability_returns_none_not_buy():
    # the old code clamped a NaN p_up to lean 1.0 (max-confidence BUY) — must be None
    assert _ml_with(float("nan")).read(_ramp()) is None
    assert _ml_with(2.0).read(_ramp()) is None        # out of [0,1] range


def test_ml_deadband_weak_edge_is_flat():
    r = _ml_with(0.52).read(_ramp())                   # |0.52-0.5| < (0.55-0.5)
    assert r is not None and r.lean == 0.0 and r.direction == "FLAT"


def test_ml_strong_edge_is_directional():
    r = _ml_with(0.9).read(_ramp())
    assert r is not None and r.lean > 0.0 and r.direction == "BUY"


def test_ml_invalid_atr_returns_none():
    # flat tape → ATR collapses to ~0 → must reject rather than emit a dead stop
    flat = {"close": [100.0] * 120, "high": [100.0] * 120, "low": [100.0] * 120}
    assert _ml_with(0.9).read(flat) is None


# ── C4 / H9 order-flow freshness + staleness ─────────────────────────────────
def test_orderflow_cold_feed_has_no_data_and_is_not_stale():
    e = OrderFlowEngine()
    assert not e.has_data and not e.stale and not e.ever_had_data


def test_orderflow_fresh_quote_has_data():
    e = OrderFlowEngine()
    e.on_depth(bid=100.0, bid_size=10, ask=100.25, ask_size=10)
    assert e.has_data and not e.stale and e.ever_had_data


def test_orderflow_frozen_feed_goes_stale_and_loses_has_data():
    e = OrderFlowEngine()
    e.on_depth(bid=100.0, bid_size=10, ask=100.25, ask_size=10)
    # backdate the last update beyond the staleness window → frozen book
    e.last_quote_ts = time.time() - (orderflow.STALENESS_SEC + 1.0)
    assert not e.has_data            # fail closed: a frozen book is NOT tradeable
    assert e.stale                   # but distinguishable from a cold feed


# ── M15 crossed / one-sided quote rejected ───────────────────────────────────
def _feed():
    from projectx_marketdata import ProjectXOrderFlowFeed

    class _MB:
        _mock_mode = True
        token = ""

    f = ProjectXOrderFlowFeed(_MB())
    f.get("ES")
    f._cid_to_sym["CID"] = "ES"
    return f


def test_crossed_quote_does_not_record_a_mark():
    f = _feed()
    eng = f.get("ES")
    f._on_quote(["CID", {"bestBid": 100.5, "bestAsk": 100.0,
                          "bestBidSize": 5, "bestAskSize": 5}])
    assert not eng.has_data           # crossed (bid>ask) rejected → no poisoned mark


def test_one_sided_quote_rejected():
    f = _feed()
    eng = f.get("ES")
    f._on_quote(["CID", {"bestBid": 100.0, "bestAsk": 0.0,
                          "bestBidSize": 5, "bestAskSize": 0}])
    assert not eng.has_data


def test_valid_two_sided_quote_accepted():
    f = _feed()
    eng = f.get("ES")
    f._on_quote(["CID", {"bestBid": 100.0, "bestAsk": 100.25,
                          "bestBidSize": 5, "bestAskSize": 5}])
    assert eng.has_data


# ── M14 kill switch flattens + H5 exit path runs end-to-end ───────────────────
class _FakeExec:
    mode = "paper"

    def close(self, pos, exit_price, state):
        state.close(pos, exit_price)


class _FakeQuote:
    def __init__(self, px):
        self.price = px


class _FakeData:
    def __init__(self, px):
        self._px = px

    def quote(self, symbol):
        return _FakeQuote(self._px)


class _FakeOFlow:
    """Inject a futures mark through the ORDER-FLOW feed — the realistic source
    of a futures price. A1 makes _mark() refuse the Alpaca equities quote for
    futures roots (ES/MNQ/…), so a futures mark must arrive via the flow feed's
    micro-price, never data.quote()."""
    def __init__(self, px):
        self._px = px

    def get(self, symbol):
        px = self._px
        return type("_OF", (), {
            "has_data": True, "micro_price": px, "bid": px, "ask": px,
        })()


def _bare_engine(mark_px: float):
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    e._topstep = None
    # Futures marks come from the order-flow feed, not the Alpaca equities quote
    # (A1). Inject the test mark there so _mark() resolves it the way it does live.
    e._oflow = _FakeOFlow(mark_px)
    e._be_state = {}                     # BE/trail ratchet state (set in __init__)
    e._exit_ambiguous = set()            # ambiguous-exit hold set (set in __init__)
    e.state = State()
    e.state.save = lambda: None          # no DB side effects in tests
    e.executor = _FakeExec()
    e.data = _FakeData(mark_px)
    return e


def test_kill_switch_flattens_all_positions():
    e = _bare_engine(5000.0)
    e.state.add(_pos("ES", "BUY", 2, 5000.0))
    e.state.add(_pos("MNQ", "SELL", 1, 18000.0))
    e._kill_switch_halt()
    assert e.state.open_positions == []   # everything flattened, not just blocked


def test_exit_path_runs_without_nameerror():
    # H5: _manage_open used an undefined `q.price` → NameError skipped state.save and
    # other positions. Drive a stop-loss exit end-to-end and confirm it books.
    e = _bare_engine(4990.0)              # mark below the stop
    pos = _pos("ES", "BUY", 1, 5000.0)
    pos.stop = 4999.0
    pos.target = 5100.0
    pos.filled = True
    e.state.add(pos)
    e._manage_open()
    assert not pos.open and pos.exit_price == pytest.approx(4990.0)
    assert pos.pnl_usd == pytest.approx(-500.0)   # -10pts * 1 * $50


# ── H-CRIT-2: ambiguous exit (OrderStateUnknown) must not be blindly retried ──
class _AmbiguousOnceExec:
    """Raises OrderStateUnknown on the FIRST close() attempt for a symbol,
    then would succeed on any later attempt — used to prove the engine holds
    the position rather than resubmitting blindly on the very next scan."""
    mode = "paper"

    def __init__(self):
        self.calls = 0

    def close(self, pos, exit_price, state):
        from projectx_executor import OrderStateUnknown
        self.calls += 1
        raise OrderStateUnknown("read timeout after send")


def test_exit_order_state_unknown_holds_position_not_retried():
    e = _bare_engine(4990.0)
    e.executor = _AmbiguousOnceExec()
    pos = _pos("ES", "BUY", 1, 5000.0)
    pos.stop = 4999.0
    pos.filled = True
    e.state.add(pos)

    e._manage_open()
    assert pos.open, "position must stay open — the exit's true state is unknown"
    assert "ES" in e._exit_ambiguous
    assert e.executor.calls == 1

    # A second scan cycle must NOT attempt another close while the symbol is
    # held for ambiguity — resubmitting here is exactly what could double-
    # execute or flip the position onto the opposite side.
    e._manage_open()
    assert e.executor.calls == 1, "must not blindly retry an ambiguous exit"
    assert pos.open


def test_exit_order_state_unknown_in_partial_close_holds_position():
    e = _bare_engine(5150.0)   # +1.5R vs a 4900 stop on a 5000 entry (100pt risk)
    pos = _pos("ES", "BUY", 4, 5000.0)
    pos.stop = 4900.0
    pos.filled = True
    e.state.add(pos)
    e._be_state = {}

    class _AmbiguousPartialExec:
        # BE-ratchet (step 1, fires first at +1.0R) amends the native stop —
        # give it a broker with no-op cancel/place so that leg is a no-op.
        broker = type("B", (), {})()

        def replace_protective_stop(self, pos, stop_price):
            pass

        def close_partial(self, pos, close_qty, exit_price):
            from projectx_executor import OrderStateUnknown
            raise OrderStateUnknown("read timeout after send")

        def close(self, pos, exit_price, state):
            raise AssertionError("full close should not run in this test")

    e.executor = _AmbiguousPartialExec()
    e._manage_partial_exit(pos, 5150.0)
    assert "ES" in e._exit_ambiguous
    assert pos.qty == 4, "qty must be unchanged — the reduction's true state is unknown"

    # A second call must be a no-op while the ambiguous hold is in effect —
    # _manage_partial_exit's own guard should return immediately.
    e._manage_partial_exit(pos, 5150.0)
    assert pos.qty == 4


def test_reconcile_clears_ambiguous_exit_hold():
    e = _bare_engine(5000.0)
    e._exit_ambiguous = {"ES"}

    class _FakeLiveBroker:
        def get_positions(self):
            return []   # broker is flat — matches nothing, but the READ succeeded

    e.executor = type("E", (), {"broker": _FakeLiveBroker()})()
    e._live_projectx = lambda: True
    e._mark = lambda sym: 5000.0

    e._reconcile_positions()
    assert e._exit_ambiguous == set(), "a successful reconcile must clear ambiguous holds"


def test_reconcile_failure_does_not_clear_ambiguous_hold():
    e = _bare_engine(5000.0)
    e._exit_ambiguous = {"ES"}

    class _FailingBroker:
        def get_positions(self):
            raise RuntimeError("network error")

    e.executor = type("E", (), {"broker": _FailingBroker()})()
    e._live_projectx = lambda: True

    e._reconcile_positions()
    assert e._exit_ambiguous == {"ES"}, (
        "a FAILED reconcile must not clear the hold — we still don't know the "
        "broker's true state, so retrying the exit would still be unsafe"
    )


# ── H-CRIT-1: live broker + Topstep risk layer attach atomically or not at all ──
class _FakeLiveBroker:
    def account(self):
        return {"equity": 50_000.0}


def test_attach_topstep_broker_failure_leaves_base_executor_attached(monkeypatch):
    import engine as eng
    import topstep_risk

    class _RaisingTopstepRiskManager:
        def __init__(self, *a, **k):
            raise RuntimeError("boom")

    monkeypatch.setattr(topstep_risk, "TopstepRiskManager", _RaisingTopstepRiskManager)

    e = eng.Engine.__new__(eng.Engine)
    e._topstep_last_reset = None
    e._topstep_day_halt = False
    base_broker = object()
    e.executor = type("E", (), {"broker": base_broker})()

    live_broker = _FakeLiveBroker()
    e._attach_topstep_broker(live_broker)

    assert e._topstep is None
    assert e.executor.broker is base_broker, (
        "a failed Topstep-risk-layer init must NEVER leave the live broker "
        "attached — that combination trades real capital with zero "
        "Topstep-specific protection"
    )


def test_attach_topstep_broker_success_attaches_both_atomically(monkeypatch):
    import engine as eng
    import topstep_risk

    class _FakeTopstepRiskManager:
        def __init__(self, initial_equity=None):
            self.initial_equity = initial_equity

        def load_day_state(self, session):
            return False, False

        def cold_start_unsafe(self):
            return False

    monkeypatch.setattr(topstep_risk, "TopstepRiskManager", _FakeTopstepRiskManager)

    e = eng.Engine.__new__(eng.Engine)
    e._topstep_last_reset = None
    e._topstep_day_halt = False
    e.executor = type("E", (), {"broker": object()})()

    live_broker = _FakeLiveBroker()
    e._attach_topstep_broker(live_broker)

    assert e._topstep is not None
    assert e.executor.broker is live_broker
