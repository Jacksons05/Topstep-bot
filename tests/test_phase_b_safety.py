"""Phase B safety tests — remediation of the 2026-07-14 overnight blow-up.

Root cause was a cascade: Postgres died → engine crash-looped → ran stateless →
re-opened forgotten positions, while native protective stops were rejected →
naked positions. Two guards:

  B-DB   : fail closed when a configured DB is unreachable (state.ping / preflight)
  B-STOP : never hold a naked futures position — flatten if no native stop confirms
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from state import Position, State


# ── B-DB: fail-closed on unreachable DB ───────────────────────────────────────

def test_ping_ok_when_stateless(monkeypatch):
    import state
    monkeypatch.setattr(state, "_DB_ENABLED", False)
    ok, detail = state.ping()
    assert ok and "stateless" in detail.lower()


def test_ping_fails_when_db_configured_but_down(monkeypatch):
    import state

    @contextmanager
    def _boom():
        raise OSError("connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(state, "_DB_ENABLED", True)
    monkeypatch.setattr(state, "_db", _boom)
    ok, detail = state.ping()
    assert not ok
    assert "refused" in detail.lower()


def test_preflight_fails_on_unreachable_db(monkeypatch):
    import preflight
    import state
    # DATABASE_URL is SET but the server is down → must be a hard FAIL so run.py
    # refuses to start (no stateless trading).
    monkeypatch.setattr(state, "DATABASE_URL", "postgresql://x@127.0.0.1:5999/x")
    monkeypatch.setattr(state, "ping", lambda: (False, "OperationalError: connection refused"))
    rep = preflight.Report()
    preflight.check_dependencies(rep)
    c = next(c for c in rep.checks if c.title == "State backend")
    assert c.status == preflight.FAIL, "DB set-but-down must FAIL preflight"


def test_preflight_passes_on_reachable_db(monkeypatch):
    import preflight
    import state
    monkeypatch.setattr(state, "DATABASE_URL", "postgresql://x@127.0.0.1:5433/x")
    monkeypatch.setattr(state, "ping", lambda: (True, "connected"))
    rep = preflight.Report()
    preflight.check_dependencies(rep)
    c = next(c for c in rep.checks if c.title == "State backend")
    assert c.status == preflight.PASS


# ── B-STOP: never hold a naked futures position ───────────────────────────────

def _pos(symbol, side, qty, entry):
    return Position(
        symbol=symbol, asset="future", side=side, qty=qty, entry_price=entry,
        size_usd=entry * qty, stop=entry * 0.99, target=entry * 1.02, kind="t",
        thesis="", opened_at="2026-07-15T00:00:00+00:00", mode="paper",
    )


class _StopRejectBroker:
    """Entry fills, but the native protective stop is always rejected (returns "")
    — the exact broker behavior that left positions naked overnight."""
    def __init__(self):
        self.submits = []

    def submit(self, symbol, qty, side, ref_price):
        from broker import Fill
        self.submits.append((symbol, qty, side))
        return Fill(symbol=symbol, qty=float(qty), side=side, price=ref_price,
                    order_id="ord-%d" % len(self.submits), status="filled")

    def place_stop_order(self, symbol, qty, side, stop_price):
        return ""  # rejected — no native stop

    def cancel_order(self, oid):
        return True


def _executor_with(broker):
    from executor import Executor
    e = Executor.__new__(Executor)
    e.broker = broker
    e.mode = "paper"
    return e


def test_flatten_unprotected_closes_position():
    """The fail-safe helper market-closes an unprotected long (SELL to flatten)
    and books it closed — no naked position left behind."""
    broker = _StopRejectBroker()
    e = _executor_with(broker)
    s = State()
    pos = _pos("MCL", "BUY", 14, 78.0)
    s.add(pos)

    e._flatten_unprotected(pos, s)

    assert not pos.open, "unprotected position must be booked closed, not held"
    assert broker.submits and broker.submits[-1][2] == "SELL", "flatten a long with a SELL"
    assert not any(p.symbol == "MCL" and p.open for p in s.open_positions)


def test_open_flattens_when_native_stop_rejected():
    """End-to-end: a futures entry whose native stop is rejected must NOT be left
    open (naked) — open() flattens it immediately."""
    from signals import Signal
    broker = _StopRejectBroker()
    e = _executor_with(broker)
    s = State()
    sig = Signal(symbol="MES", asset="future", side="BUY", price=5000.0,
                 confidence=0.7, kind="confluence", atr=10.0)

    pos = e.open(sig, 0.0, s, risk_mult=1.0, max_contracts=5)

    # A futures entry was placed, the stop was rejected, so the real position must
    # be flat (booked closed) — never naked.
    assert pos is not None
    assert not pos.open, "a position with no confirmed native stop must be flattened"
    real = [p for p in s.open_positions if not p.shadow and p.symbol == "MES"]
    assert not real, "no naked MES futures position may remain open"
    # entry BUY then emergency close SELL both hit the broker
    sides = [sd for (_sym, _q, sd) in broker.submits]
    assert "BUY" in sides and "SELL" in sides
