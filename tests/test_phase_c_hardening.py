"""Phase C hardening tests: Bug #7 peak reseed, mid-session DB fail-closed,
server-side brackets + stop collar, and the GEX-regime entry pivot.

Naming follows test_phase_a_safety.py / test_phase_b_safety.py.
"""
from __future__ import annotations

import dataclasses
import math

import pytest

from config import CONFIG


# ── Phase 1: peak_balance reseed (Bug #7) ────────────────────────────────────

class TestPeakReseed:
    def test_historical_peak_beats_drawdown_balance(self):
        """Cold boot in a drawdown: the DB's historical max must win, restoring
        the true (locked) MLL floor instead of one $2k below the drawn-down
        balance."""
        from topstep_risk import TopstepRiskManager
        ts = TopstepRiskManager(initial_equity=48_620.55, historical_peak=100_200.0)
        assert ts.peak_equity == 100_200.0
        # Floor locks at the account size once peak ≥ size + buffer.
        assert ts.mll_floor() == CONFIG.topstep_account_size

    def test_no_history_falls_back_to_balance_seed(self):
        """historical_peak=None (a genuinely fresh account/cycle) preserves the
        original seed semantics: max(balance, account_size)."""
        from topstep_risk import TopstepRiskManager
        ts = TopstepRiskManager(initial_equity=48_620.55, historical_peak=None)
        assert ts.peak_equity == CONFIG.topstep_account_size
        ts2 = TopstepRiskManager(initial_equity=51_000.0, historical_peak=None)
        assert ts2.peak_equity == 51_000.0

    def test_history_never_lowers_the_seed(self):
        """A LOWER historical value (stale row from before a profitable run)
        must never drag the peak below the balance/account seed — every source
        is a ratchet."""
        from topstep_risk import TopstepRiskManager
        ts = TopstepRiskManager(initial_equity=53_000.0, historical_peak=49_000.0)
        assert ts.peak_equity == 53_000.0

    def test_record_equity_rejects_junk(self, monkeypatch):
        """NaN/inf/≤0 must never be written into the history the peak reseeds
        from (a poisoned max would inflate the floor forever)."""
        import state as state_mod
        calls = []
        monkeypatch.setattr(state_mod, "_DB_ENABLED", True)
        monkeypatch.setattr(state_mod, "_ensure_schema", lambda: None)
        monkeypatch.setattr(state_mod, "_db", lambda: (_ for _ in ()).throw(
            AssertionError("junk equity reached the DB")))
        for bad in (float("nan"), float("inf"), -1.0, 0.0):
            state_mod.record_equity("acct-1", bad)  # must not raise, must not write
        assert calls == []


# ── Phase 2: mid-session DB loss → flatten + hard exit ───────────────────────

class _PanicBroker:
    def __init__(self, fail_times: int = 0):
        self.flatten_calls = 0
        self._fail_times = fail_times

    def flatten_all(self):
        self.flatten_calls += 1
        if self.flatten_calls <= self._fail_times:
            raise RuntimeError("gateway down")
        return {}


class _PanicEngine:
    """Bare object carrying just what the panic path touches."""
    from engine import Engine as _E
    _db_panic_flatten_and_exit = _E._db_panic_flatten_and_exit
    _db_heartbeat = _E._db_heartbeat

    def __init__(self, broker):
        class _Exec:
            pass
        self.executor = _Exec()
        self.executor.broker = broker


def test_db_panic_flattens_then_exits(monkeypatch):
    import engine as engine_mod
    monkeypatch.setattr(engine_mod, "notify", lambda *a, **k: None)
    broker = _PanicBroker()
    e = _PanicEngine(broker)
    with pytest.raises(SystemExit) as exc:
        e._db_panic_flatten_and_exit("test: connection refused")
    assert exc.value.code == 1
    assert broker.flatten_calls == 1


def test_db_panic_retries_flatten_and_exits_even_on_total_failure(monkeypatch):
    """Flatten failing every attempt must still end in exit(1) — trading on
    is never the fallback."""
    import engine as engine_mod
    monkeypatch.setattr(engine_mod, "notify", lambda *a, **k: None)
    monkeypatch.setattr(engine_mod.time, "sleep", lambda s: None)
    broker = _PanicBroker(fail_times=99)
    e = _PanicEngine(broker)
    with pytest.raises(SystemExit) as exc:
        e._db_panic_flatten_and_exit("test: connection refused")
    assert exc.value.code == 1
    assert broker.flatten_calls == 3  # bounded retries


def test_heartbeat_panics_when_db_configured_but_down(monkeypatch):
    import engine as engine_mod
    import state as state_mod
    monkeypatch.setattr(engine_mod, "notify", lambda *a, **k: None)
    monkeypatch.setattr(state_mod, "DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(state_mod, "ping",
                        lambda: (False, "OperationalError: refused"))
    broker = _PanicBroker()
    e = _PanicEngine(broker)
    with pytest.raises(SystemExit):
        e._db_heartbeat()
    assert broker.flatten_calls == 1


def test_heartbeat_passes_stateless_and_healthy(monkeypatch):
    import engine as engine_mod
    import state as state_mod
    monkeypatch.setattr(engine_mod, "notify", lambda *a, **k: None)
    e = _PanicEngine(_PanicBroker())
    monkeypatch.setattr(state_mod, "DATABASE_URL", "")
    e._db_heartbeat()  # stateless: deliberate mode, no panic
    monkeypatch.setattr(state_mod, "DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(state_mod, "ping", lambda: (True, "connected"))
    e._db_heartbeat()  # healthy: no panic


def test_state_load_raises_on_nonfinite_day_anchor(monkeypatch):
    """A NaN day_start_pnl silently disarms the DLL (NaN comparisons are all
    False) — load must fail closed instead."""
    import state as state_mod

    class _Cur:
        def __init__(self):
            self._q = ""

        def execute(self, q, *a):
            self._q = q

        def fetchone(self):
            return (0.0, 0.0, "2026-07-16", float("nan"), "")

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    from contextlib import contextmanager

    @contextmanager
    def _fake_db():
        yield _Conn()

    monkeypatch.setattr(state_mod, "DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(state_mod, "_db", _fake_db)
    monkeypatch.setattr(state_mod, "_ensure_schema", lambda: None)
    with pytest.raises(RuntimeError, match="state_meta corrupt"):
        state_mod.State.load()


# ── Phase 3: server-side brackets + stop collar + regime ATR mult ────────────

class TestStopCollar:
    def test_sell_stop_above_mark_is_collared_below(self):
        from projectx_executor import ProjectXBroker
        px, clamped = ProjectXBroker.collar_stop_price("SELL", 5001.0, 5000.0, 0.25)
        assert clamped and px == pytest.approx(4999.75)

    def test_buy_stop_below_mark_is_collared_above(self):
        from projectx_executor import ProjectXBroker
        px, clamped = ProjectXBroker.collar_stop_price("BUY", 4999.0, 5000.0, 0.25)
        assert clamped and px == pytest.approx(5000.25)

    def test_valid_stops_pass_untouched(self):
        from projectx_executor import ProjectXBroker
        px, clamped = ProjectXBroker.collar_stop_price("SELL", 4990.0, 5000.0, 0.25)
        assert not clamped and px == 4990.0
        px, clamped = ProjectXBroker.collar_stop_price("BUY", 5010.0, 5000.0, 0.25)
        assert not clamped and px == 5010.0

    def test_degenerate_mark_or_tick_is_a_noop(self):
        from projectx_executor import ProjectXBroker
        assert ProjectXBroker.collar_stop_price("SELL", 5001.0, 0.0, 0.25) == (5001.0, False)
        assert ProjectXBroker.collar_stop_price("SELL", 5001.0, 5000.0, 0.0) == (5001.0, False)


class TestRegimeAtrMult:
    def _sig(self, price=5000.0, atr=10.0, side="BUY"):
        from signals import Signal
        from signals import label_for
        return Signal(symbol="MES", asset="future", side=side, price=price,
                      confidence=0.8, confidence_label=label_for(0.8),
                      thesis="t", quant=0.5, qual=0.5, atr=atr, agents={})

    def test_atr_mult_overrides_global(self):
        from executor import futures_plan
        sig = self._sig()
        base = futures_plan(sig, sig.price)
        tight = futures_plan(sig, sig.price, atr_mult=1.0)
        assert base is not None and tight is not None
        assert tight.stop_distance_points == pytest.approx(1.0 * sig.atr)
        assert base.stop_distance_points == pytest.approx(CONFIG.atr_stop_mult * sig.atr)

    def test_junk_atr_mult_falls_back(self):
        from executor import futures_plan
        sig = self._sig()
        for junk in (0.0, -1.0, float("nan")):
            p = futures_plan(sig, sig.price, atr_mult=junk)
            assert p is not None
            assert p.stop_distance_points == pytest.approx(CONFIG.atr_stop_mult * sig.atr)


class _BracketBroker:
    """Broker double for the bracket-adoption path."""

    def __init__(self, child: dict | None):
        self._child = child
        self.submitted = []
        self.cancelled = []
        self.flattened = []

    def submit(self, symbol, qty, side, ref_price, stop_loss_ticks=None):
        from broker import Fill
        self.submitted.append({"symbol": symbol, "qty": qty, "side": side,
                               "stop_loss_ticks": stop_loss_ticks})
        return Fill(symbol=symbol, qty=float(int(qty)), side=side,
                    price=ref_price, order_id="entry-1", status="filled")

    def find_bracket_stop(self, entry_order_id, tries=5, delay_s=0.5):
        return self._child

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def get_fill(self, order_id):
        return "filled", None

    def close(self):
        pass


def _mk_executor(broker):
    from executor import Executor
    ex = Executor.__new__(Executor)  # skip __init__ (would build a real broker)
    ex.broker = broker
    ex.mode = "paper"
    return ex


def _entry_sig(price=5000.0, atr=10.0, side="BUY"):
    from signals import Signal, label_for
    return Signal(symbol="MES", asset="future", side=side, price=price,
                  confidence=0.8, confidence_label=label_for(0.8),
                  thesis="t", quant=0.5, qual=0.5, atr=atr, agents={})


class TestBracketAdoption:
    def test_entry_carries_stop_loss_ticks(self, monkeypatch):
        from state import State
        import executor as executor_mod
        monkeypatch.setattr(executor_mod, "CONFIG",
                            dataclasses.replace(CONFIG, px_bracket_enabled=True))
        sig = _entry_sig()
        # valid child: SELL stop below the fill for a BUY entry
        broker = _BracketBroker({"order_id": "stop-9", "stop_price": 4980.0,
                                 "side": "SELL", "size": 1})
        ex = _mk_executor(broker)
        st = State(day="2026-07-16")
        pos = ex.open(sig, 1000.0, st)
        assert pos is not None
        sent = broker.submitted[0]
        # MES tick 0.25: 2×ATR=20pts → 80 ticks
        assert sent["stop_loss_ticks"] == 80
        assert pos.protective_order_id == "stop-9"
        assert pos.stop == pytest.approx(4980.0)  # books the ACTUAL resting stop
        assert pos.open

    def test_missing_bracket_child_flattens(self, monkeypatch):
        from state import State
        import executor as executor_mod
        monkeypatch.setattr(executor_mod, "CONFIG",
                            dataclasses.replace(CONFIG, px_bracket_enabled=True))
        sig = _entry_sig()
        broker = _BracketBroker(None)  # no child visible → treat as unprotected
        ex = _mk_executor(broker)
        st = State(day="2026-07-16")
        pos = ex.open(sig, 1000.0, st)
        assert pos is not None
        assert not pos.open  # flattened immediately
        # crucial: no second stop was placed (double-stop flip risk)
        assert broker.cancelled == []

    def test_wrong_side_child_is_cancelled_and_flattened(self, monkeypatch):
        from state import State
        import executor as executor_mod
        monkeypatch.setattr(executor_mod, "CONFIG",
                            dataclasses.replace(CONFIG, px_bracket_enabled=True))
        sig = _entry_sig()
        # invalid: stop ABOVE fill for a long (tick-convention misread)
        broker = _BracketBroker({"order_id": "stop-9", "stop_price": 5020.0,
                                 "side": "SELL", "size": 1})
        ex = _mk_executor(broker)
        st = State(day="2026-07-16")
        pos = ex.open(sig, 1000.0, st)
        assert pos is not None
        assert not pos.open
        assert broker.cancelled == ["stop-9"]

    def test_bracket_disabled_uses_legacy_stop_path(self, monkeypatch):
        from state import State
        import executor as executor_mod
        monkeypatch.setattr(executor_mod, "CONFIG",
                            dataclasses.replace(CONFIG, px_bracket_enabled=False))
        sig = _entry_sig()
        broker = _BracketBroker({"order_id": "stop-9", "stop_price": 4980.0,
                                 "side": "SELL", "size": 1})
        placed = []
        broker.place_stop_order = lambda sym, qty, side, stop, mark=None: (
            placed.append((sym, side, stop, mark)) or "legacy-stop-1")
        ex = _mk_executor(broker)
        st = State(day="2026-07-16")
        pos = ex.open(sig, 1000.0, st)
        assert pos is not None
        assert broker.submitted[0]["stop_loss_ticks"] is None
        assert placed and placed[0][3] == pytest.approx(5000.0)  # mark = fill
        assert pos.protective_order_id == "legacy-stop-1"


# ── Phase 4: GEX regime classification + entry signals ───────────────────────

class TestGexClassification:
    def test_bands(self):
        from uw_gex import classify_gex
        hist = [100.0, 120.0, 80.0, 110.0, 90.0]  # median 100 → band 25 @ 0.25
        assert classify_gex(60.0, hist, 0.25) == "positive"
        assert classify_gex(-60.0, hist, 0.25) == "negative"
        assert classify_gex(10.0, hist, 0.25) == "neutral"
        assert classify_gex(-10.0, hist, 0.25) == "neutral"

    def test_empty_history_classifies_by_sign(self):
        from uw_gex import classify_gex
        assert classify_gex(1.0, [], 0.25) == "positive"
        assert classify_gex(-1.0, [], 0.25) == "negative"
        assert classify_gex(0.0, [], 0.25) == "neutral"


def _bars(closes, highs=None, lows=None):
    highs = highs or [c + 2 for c in closes]
    lows = lows or [c - 2 for c in closes]
    return {"close": list(closes), "high": list(highs), "low": list(lows),
            "volume": [100] * len(closes)}


class TestGexStrategy:
    def test_neutral_locks_entries(self):
        from gex_strategy import gex_quant_signal
        bars = _bars([5000 + i * 0.1 for i in range(60)])
        assert gex_quant_signal(bars, "neutral", 5000.0) is None

    def test_positive_gamma_fades_stretch_below_vwap(self):
        from gex_strategy import gex_quant_signal
        closes = [5000.0] * 59 + [4970.0]   # last close well below vwap
        bars = _bars(closes)
        read = gex_quant_signal(bars, "positive", 5000.0)
        assert read is not None and read.lean > 0  # revert LONG toward vwap
        assert "VWAP-MR" in read.detail

    def test_positive_gamma_fades_stretch_above_vwap(self):
        from gex_strategy import gex_quant_signal
        closes = [5000.0] * 59 + [5030.0]
        bars = _bars(closes)
        read = gex_quant_signal(bars, "positive", 5000.0)
        assert read is not None and read.lean < 0  # revert SHORT

    def test_positive_gamma_no_stretch_no_entry(self):
        from gex_strategy import gex_quant_signal
        closes = [5000.0] * 60
        bars = _bars(closes)
        assert gex_quant_signal(bars, "positive", 5000.0) is None

    def test_negative_gamma_breakout_long(self):
        from gex_strategy import gex_quant_signal
        closes = [5000.0] * 59 + [5015.0]
        highs = [5005.0] * 59 + [5016.0]    # prior high 5005, close 5015 above it
        lows = [4995.0] * 60
        bars = _bars(closes, highs, lows)
        read = gex_quant_signal(bars, "negative", None)
        assert read is not None and read.lean > 0
        assert "breakout" in read.detail

    def test_negative_gamma_breakdown_short(self):
        from gex_strategy import gex_quant_signal
        closes = [5000.0] * 59 + [4985.0]
        highs = [5005.0] * 60
        lows = [4995.0] * 59 + [4984.0]
        bars = _bars(closes, highs, lows)
        read = gex_quant_signal(bars, "negative", None)
        assert read is not None and read.lean < 0

    def test_negative_gamma_inside_range_no_entry(self):
        from gex_strategy import gex_quant_signal
        closes = [5000.0] * 60
        bars = _bars(closes)
        assert gex_quant_signal(bars, "negative", None) is None

    def test_positive_without_vwap_fails_closed(self):
        from gex_strategy import gex_quant_signal
        closes = [5000.0] * 59 + [4970.0]
        assert gex_quant_signal(_bars(closes), "positive", None) is None

    def test_unknown_regime_fails_closed(self):
        from gex_strategy import gex_quant_signal
        closes = [5000.0] * 59 + [4970.0]
        assert gex_quant_signal(_bars(closes), "weird", 5000.0) is None
