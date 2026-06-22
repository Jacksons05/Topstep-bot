"""Regression tests for the six Topstep money-path fixes (2→1→3→4→5→6).

These lock in the account-survival behavior:
  * #2 futures contract multiplier in realized P&L
  * #3 ProjectX market orders report filled (so positions get managed)
  * #5/#6 daily-loss + trailing-MLL trip at the CORRECT dollar equity
  * #6 session-date rolls at 18:00 ET, not server midnight

They are engine-light on purpose (no network): state/risk/broker units plus one
patched session-date check.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from config import CONFIG
from state import Position, State
from topstep_risk import TopstepRiskManager


def _pos(symbol: str, side: str, qty: float, entry: float) -> Position:
    return Position(
        symbol=symbol, asset="future", side=side, qty=qty, entry_price=entry,
        size_usd=entry * qty, stop=0.0, target=0.0, kind="test", thesis="",
        opened_at="2026-06-22T12:00:00+00:00", mode="paper",
    )


# ── #2 contract multiplier ───────────────────────────────────────────────────
def test_es_pnl_uses_dollar_multiplier():
    # ES: $50/point. Long 1 contract, +2 points → $100, NOT $2.
    s = State()
    pos = _pos("ES", "BUY", 1, 5000.0)
    s.add(pos)
    s.close(pos, 5002.0)
    assert pos.pnl_usd == pytest.approx(100.0)
    assert s.realized_pnl_usd == pytest.approx(100.0)


def test_es_short_pnl_multiplier():
    s = State()
    pos = _pos("ES", "SELL", 2, 5000.0)
    s.add(pos)
    s.close(pos, 4998.0)  # short, price down 2pts, 2 contracts → +2*2*50 = $200
    assert pos.pnl_usd == pytest.approx(200.0)


def test_mnq_multiplier():
    # MNQ: $2/point. Long 1, +10 points → $20.
    s = State()
    pos = _pos("MNQ", "BUY", 1, 18000.0)
    s.add(pos)
    s.close(pos, 18010.0)
    assert pos.pnl_usd == pytest.approx(20.0)


def test_equity_symbol_unchanged_multiplier_one():
    # Unknown/equity root → multiplier 1.0 → legacy behavior preserved.
    s = State()
    pos = _pos("AAPL", "BUY", 10, 150.0)
    s.add(pos)
    s.close(pos, 151.0)  # +$1 * 10 shares = $10
    assert pos.pnl_usd == pytest.approx(10.0)


# ── #5/#6 trailing MLL trips at the correct dollar equity ────────────────────
def test_trailing_mll_floor_locks_at_start_balance():
    acct = CONFIG.topstep_account_size      # 50_000
    buf = CONFIG.topstep_trailing_mll       # 2_000
    m = TopstepRiskManager(initial_equity=acct)
    # before lock: peak 51_000 → floor = 51_000 - 2_000 = 49_000
    m.update_equity(acct + 1_000)
    assert m.mll_floor() == pytest.approx(acct - buf + 1_000)
    # peak ≥ start + buffer → floor LOCKS at the start balance (50_000)
    m.update_equity(acct + buf + 500)
    assert m.mll_floor() == pytest.approx(acct)


def test_trailing_mll_breaches_just_below_locked_floor():
    acct = CONFIG.topstep_account_size
    buf = CONFIG.topstep_trailing_mll
    m = TopstepRiskManager(initial_equity=acct)
    m.update_equity(acct + buf + 1_000)     # lock floor at acct (50_000)
    ok, _ = m.trailing_mll_ok(acct + 1.0)   # $1 above floor → safe
    assert ok
    ok, why = m.trailing_mll_ok(acct - 1.0)  # $1 below locked floor → breach
    assert not ok and "MLL" in why


def test_multiplier_bug_would_have_hidden_this():
    # Proof the fix matters: with the OLD points-scale P&L a 40-point ES loss on
    # 1 contract booked -$40 (never near the $2k floor); with the multiplier it's
    # -$2,000 of real equity — exactly what the MLL must see.
    s = State()
    pos = _pos("ES", "BUY", 1, 5000.0)
    s.add(pos)
    s.close(pos, 4960.0)  # -40 points
    assert pos.pnl_usd == pytest.approx(-2000.0)
    acct = CONFIG.topstep_account_size
    m = TopstepRiskManager(initial_equity=acct)
    equity = acct + s.realized_pnl_usd       # 48_000
    ok, _ = m.trailing_mll_ok(equity)        # floor = 50_000-2_000 = 48_000
    assert not ok                            # touches the floor → breach


# ── #6 daily loss limit anchored to session equity ──────────────────────────
def test_daily_loss_trips_at_exact_dollar_limit():
    acct = CONFIG.topstep_account_size
    limit = CONFIG.topstep_daily_loss_limit  # 1_000
    m = TopstepRiskManager(initial_equity=acct)
    m.reset_day(acct)                        # anchor day_start_equity = 50_000
    ok, _ = m.daily_loss_ok(acct - limit + 1.0)   # -$999 → still ok
    assert ok
    ok, why = m.daily_loss_ok(acct - limit)       # -$1,000 → day done
    assert not ok and "daily loss" in why.lower()


def test_daily_loss_independent_of_state_utc_roll():
    # equity-anchored measure ignores state.daily_pnl()'s UTC roll entirely
    acct = CONFIG.topstep_account_size
    m = TopstepRiskManager(initial_equity=acct)
    m.reset_day(acct + 500)                  # session started up $500
    # now equity is back to flat (acct): day_pnl = acct - (acct+500) = -500
    ok, _ = m.daily_loss_ok(acct)
    assert ok                                # -$500 within the $1k limit


# ── #3 ProjectX market order reports filled ──────────────────────────────────
def test_projectx_mock_submit_reports_filled():
    from projectx_executor import ProjectXBroker
    b = ProjectXBroker()                     # no creds → mock mode, no network
    fill = b.submit("ES", 1, "BUY", 5000.0)
    assert fill.status == "filled"           # was "accepted" → positions never managed


# ── #6 session date rolls at 18:00 ET ────────────────────────────────────────
def test_session_date_rolls_at_18_et(monkeypatch):
    import engine as eng

    class _FakeDT:
        @staticmethod
        def now(tz=None):
            # 18:30 ET on 2026-06-22 → belongs to the 2026-06-23 session
            return datetime(2026, 6, 22, 18, 30, tzinfo=ZoneInfo("America/New_York"))

    e = eng.Engine.__new__(eng.Engine)       # no __init__ → no network/broker
    monkeypatch.setattr(eng, "datetime", _FakeDT)
    assert e._topstep_session_date() == date(2026, 6, 23)


def test_session_date_before_18_et_is_today(monkeypatch):
    import engine as eng

    class _FakeDT:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 6, 22, 10, 0, tzinfo=ZoneInfo("America/New_York"))

    e = eng.Engine.__new__(eng.Engine)
    monkeypatch.setattr(eng, "datetime", _FakeDT)
    assert e._topstep_session_date() == date(2026, 6, 22)
