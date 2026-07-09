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
def test_projectx_mock_submit_reports_filled(monkeypatch):
    # FORCE mock mode: with real creds in .env, ProjectXBroker() logs in live
    # and this test placed REAL 1-lot ES market orders on the account every
    # pytest run (discovered 2026-07-03 — the "unattributed foreign ES orders").
    # CONFIG is frozen, so swap the module's reference for a credless copy.
    import dataclasses
    import projectx_executor as px
    monkeypatch.setattr(
        px, "CONFIG",
        dataclasses.replace(px.CONFIG, projectx_username="", projectx_api_key=""))
    b = px.ProjectXBroker()                  # no creds → mock mode, no network
    assert b._mock_mode, "test must never talk to the live gateway"
    fill = b.submit("ES", 1, "BUY", 5000.0)
    assert fill.status == "filled"           # was "accepted" → positions never managed


# ── dashboard futures P&L: no bogus Alpaca stock quote for futures roots ─────
def test_dashboard_skips_alpaca_quote_for_futures(monkeypatch):
    # A futures root like "ES" collides with a real NYSE ticker (Eversource), so
    # quoting it via Alpaca stocks would show a bogus price + wrong unrealized.
    import dashboard
    import marketdata as md_mod
    import state as state_mod

    st = State()
    st.add(_pos("ES", "BUY", 2, 5000.0))
    monkeypatch.setattr(state_mod.State, "load", classmethod(lambda cls: st))

    class _MD:
        def __init__(self, *a, **k):
            pass

        def quotes(self, syms):
            raise AssertionError(f"futures must not be quoted via Alpaca stocks: {syms}")

        def close(self):
            pass

    monkeypatch.setattr(md_mod, "MarketData", _MD)

    out = dashboard._fetch_positions_from_db()
    assert out["open"][0]["current_price"] is None      # futures not mispriced
    assert out["open"][0]["unrealized_pnl"] is None


# ── Cramer shadow book mirror-closes with the real position ──────────────────
def test_closing_real_position_mirror_closes_cramer_shadow():
    # The inverse shadow has no stop/target, so should_exit never fires on it and
    # no flatten path touches it. Closing the real leg must mirror-close its
    # shadow so shadow_pnl_usd (dashboard + day_learner) actually reflects the
    # "what if we inverted" book instead of being stuck at 0.
    s = State()
    real = _pos("ES", "BUY", 1, 5000.0)
    real.stop, real.target = 4990.0, 5020.0
    shadow = Position(
        symbol="ES", asset="future", side="SELL", qty=1, entry_price=5000.0,
        size_usd=5000.0, stop=0.0, target=0.0, kind="cramer", thesis="inverse shadow",
        opened_at="2026-06-22T12:00:01+00:00", mode="paper", shadow=True,
    )
    s.add(real)
    s.add(shadow)
    s.close(real, 5002.0)                       # real long +2pts → +$100
    assert not real.open and real.pnl_usd == pytest.approx(100.0)
    assert not shadow.open                      # mirror-closed alongside the real leg
    assert shadow.pnl_usd == pytest.approx(-100.0)   # inverse book takes the other side
    assert s.shadow_pnl_usd == pytest.approx(-100.0)
    assert s.open_shadow == []


def test_closing_real_position_without_shadow_is_unaffected():
    # No shadow present (CRAMER_MODE off) → close behaves exactly as before.
    s = State()
    real = _pos("MNQ", "BUY", 1, 18000.0)
    s.add(real)
    s.close(real, 18010.0)
    assert not real.open and real.pnl_usd == pytest.approx(20.0)
    assert s.shadow_pnl_usd == 0.0


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


# ── Reconcile float-equality tolerance (correctness audit finding) ────────────

def test_reconcile_exact_match_survives_float_noise(monkeypatch):
    """A broker qty of 2.0000001 must match a local qty of 2 — float noise from
    the broker's JSON parser must never cause a phantom-close of a real position."""
    import engine as eng
    from state import Position

    e = eng.Engine.__new__(eng.Engine)
    e._topstep = None
    e._exit_ambiguous = set()
    e.state = State()
    e.state.save = lambda: None

    pos = Position(
        symbol="MES", asset="future", side="BUY", qty=2,
        entry_price=5000.0, size_usd=10_000.0, stop=0.0, target=0.0,
        kind="test", thesis="", opened_at="2026-01-01T00:00:00+00:00", mode="paper",
    )
    e.state.add(pos)

    class _FakeBroker:
        def get_positions(self):
            # Broker returns the same 2-contract position, but as a float
            # with a tiny precision error that exact == comparison would fail on.
            return [{"symbol": "MES", "side": "BUY", "qty": 2.0000001,
                     "avg_price": 5000.0, "contract_id": "CID"}]

    e.executor = type("E", (), {"broker": _FakeBroker()})()
    e._live_projectx = lambda: True
    e._mark = lambda sym: 5000.0

    e._reconcile_positions()
    assert pos.open, "position must survive — float noise must not cause phantom-close"
    assert e.state.open_positions == [pos]


def test_reconcile_real_size_change_still_adopted(monkeypatch):
    """A materially different broker qty (1 vs 2 contracts) must still be
    detected and adopted — the tolerance must not swallow genuine size drift."""
    import engine as eng
    from state import Position

    e = eng.Engine.__new__(eng.Engine)
    e._topstep = None
    e._exit_ambiguous = set()
    e.state = State()
    e.state.save = lambda: None

    pos = Position(
        symbol="MES", asset="future", side="BUY", qty=2,
        entry_price=5000.0, size_usd=10_000.0, stop=0.0, target=0.0,
        kind="test", thesis="", opened_at="2026-01-01T00:00:00+00:00", mode="paper",
    )
    e.state.add(pos)

    class _FakeBroker:
        def get_positions(self):
            # Broker says only 1 contract remains (partial close on exchange).
            return [{"symbol": "MES", "side": "BUY", "qty": 1.0,
                     "avg_price": 5000.0, "contract_id": "CID"}]

    e.executor = type("E", (), {"broker": _FakeBroker()})()
    e._live_projectx = lambda: True
    e._mark = lambda sym: 5000.0

    e._reconcile_positions()
    assert pos.qty == pytest.approx(1.0), "broker qty must be adopted for a genuine size change"


# ── day_state.json corruption defense ─────────────────────────────────────────

def test_load_day_state_ignores_non_dict_json(tmp_path, monkeypatch):
    """Valid JSON that is not a dict (null, list, string) must be treated as
    absent rather than raising AttributeError on d.get()."""
    ts = TopstepRiskManager.__new__(TopstepRiskManager)
    ts.peak_equity = 50_000.0
    ts.day_start_equity = 50_000.0

    for bad_content in ("null", "[]", '"string"', "42"):
        state_file = tmp_path / f"day_state_{bad_content[:5]}.json"
        state_file.write_text(bad_content)
        monkeypatch.setattr(TopstepRiskManager, "_DAY_STATE_FILE",
                            type("P", (), {"read_text": lambda self: bad_content,
                                           "exists": lambda self: True})())
        # Patch at instance level since _DAY_STATE_FILE is a class variable.
        ts._DAY_STATE_FILE = state_file
        restored, halt = ts.load_day_state("2026-07-09")
        assert not restored, f"non-dict JSON {bad_content!r} must return (False, False)"
        assert not halt


def test_load_day_state_tolerates_truncated_file(tmp_path):
    """A truncated / syntactically invalid file must be silently ignored."""
    ts = TopstepRiskManager.__new__(TopstepRiskManager)
    ts.peak_equity = 50_000.0
    ts.day_start_equity = 50_000.0
    state_file = tmp_path / "day_state.json"
    state_file.write_text('{"session_date": "2026-07-09", "peak_equity": 51000')  # truncated
    ts._DAY_STATE_FILE = state_file

    restored, halt = ts.load_day_state("2026-07-09")
    assert not restored


def test_load_day_state_peak_equity_wrong_type_does_not_raise(tmp_path):
    """peak_equity stored as a string must not crash load_day_state."""
    ts = TopstepRiskManager.__new__(TopstepRiskManager)
    ts.peak_equity = 50_000.0
    ts.day_start_equity = 50_000.0
    state_file = tmp_path / "day_state.json"
    state_file.write_text(
        '{"session_date": "2026-07-09", "peak_equity": "corrupt", '
        '"day_start_equity": 50000.0, "day_halt": false}'
    )
    ts._DAY_STATE_FILE = state_file
    # Must not raise; peak_equity corruption → keeps current value
    restored, halt = ts.load_day_state("2026-07-09")
    assert restored  # session matched
    assert ts.peak_equity == pytest.approx(50_000.0)  # corrupt field → ignored, kept original
