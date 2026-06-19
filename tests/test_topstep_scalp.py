"""Topstep microscalping guard — deterministic unit tests (no network/broker).

Covers the two-part defense added to TopstepRiskManager:
  1. soft min-hold on take-profit exits (profit_exit_held_long_enough / hold_seconds)
  2. profit-share attribution that blocks new entries (record_close / scalp_profit_ok)
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CONFIG  # noqa: E402
from topstep_risk import TopstepRiskManager, hold_seconds  # noqa: E402


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _mgr() -> TopstepRiskManager:
    # explicit equity so the test never depends on CONFIG.bankroll_usd
    return TopstepRiskManager(initial_equity=50_000.0)


# ── hold_seconds ──────────────────────────────────────────

def test_hold_seconds_recent():
    assert 0.0 <= hold_seconds(_iso_ago(2)) < 5.0


def test_hold_seconds_old():
    assert hold_seconds(_iso_ago(60)) >= 59.0


def test_hold_seconds_bad_timestamp_fails_open():
    # +inf => treated as "held long enough", never traps an exit
    assert hold_seconds("not-a-date") == float("inf")
    assert hold_seconds("x") == float("inf")


# ── soft min-hold gate ────────────────────────────────────

def test_profit_exit_blocked_when_too_fresh():
    m = _mgr()
    assert m.profit_exit_held_long_enough(_iso_ago(1)) is False


def test_profit_exit_allowed_after_min_hold():
    m = _mgr()
    assert m.profit_exit_held_long_enough(_iso_ago(10)) is True


# ── profit-share attribution ──────────────────────────────

def test_losers_excluded_from_pool():
    m = _mgr()
    m.record_close(-200.0, held_sec=1.0)   # fast loser — ignored
    m.record_close(300.0, held_sec=60.0)   # slow winner
    assert m.scalp_profit_share() == 0.0


def test_scalp_share_math():
    m = _mgr()
    m.record_close(100.0, held_sec=2.0)    # ≤5s winner
    m.record_close(300.0, held_sec=120.0)  # slow winner
    # 100 / (100+300) = 0.25
    assert abs(m.scalp_profit_share() - 0.25) < 1e-9


def test_scalp_ok_under_limit():
    m = _mgr()
    m.record_close(100.0, held_sec=2.0)
    m.record_close(900.0, held_sec=120.0)  # share = 0.10
    ok, _ = m.scalp_profit_ok()
    assert ok is True


def test_scalp_blocks_at_limit():
    m = _mgr()
    # share = 0.50 ≥ default limit 0.40 → blocked
    m.record_close(500.0, held_sec=2.0)
    m.record_close(500.0, held_sec=120.0)
    ok, reason = m.scalp_profit_ok()
    assert ok is False
    assert "microscalp" in reason.lower()


def test_reset_day_clears_attribution():
    m = _mgr()
    m.record_close(500.0, held_sec=2.0)
    m.reset_day(50_000.0)
    assert m.scalp_profit_share() == 0.0
    assert m.scalp_profit_ok()[0] is True


def test_boundary_hold_counts_as_scalp():
    # held_sec exactly at the limit counts as a ≤Ns scalp (inclusive)
    m = _mgr()
    m.record_close(100.0, held_sec=CONFIG.topstep_min_profit_hold_sec)
    assert m.scalp_profit_share() == 1.0


# ── manual trade tickets (signal-only bridge) ─────────────

def _ticket_pos(side="BUY"):
    from state import Position
    return Position(
        symbol="MES", asset="future", side=side, qty=2, entry_price=7604.25,
        size_usd=15208.5, stop=7598.25, target=7616.25, kind="confluence",
        thesis="order-flow long: OBI+ CVD+ above VWAP", opened_at="x", mode="paper",
        pnl_usd=120.0,
    )


def test_trade_ticket_long():
    from notifier import trade_ticket
    t = trade_ticket(_ticket_pos("BUY"), 0.72, "high")
    assert "LONG" in t and "BUY 2 MES" in t
    assert "STOP 7598.25" in t and "TARGET 7616.25" in t
    assert "R:R 2.0" in t  # reward 12.0 / risk 6.0


def test_trade_ticket_short():
    from notifier import trade_ticket
    p = _ticket_pos("SELL")
    p.stop, p.target = 7610.25, 7592.25
    t = trade_ticket(p, 0.6, "medium")
    assert "SHORT" in t and "SELL 2 MES" in t


def test_exit_ticket_flips_side():
    from notifier import exit_ticket
    # closing a long => SELL
    assert "SELL 2 MES" in exit_ticket(_ticket_pos("BUY"), 7616.25, "take-profit")
    # closing a short => BUY
    assert "BUY 2 MES" in exit_ticket(_ticket_pos("SELL"), 7592.25, "take-profit")
