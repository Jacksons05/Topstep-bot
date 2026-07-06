"""Quick-win hardening tests (2026-07-05 review):

  * dashboard control endpoints (/api/stop, /api/restart) require CONFIGURED auth
  * day_learner never lowers an operator entry-halt (threshold >= 1.0)
  * cramer flip needs n >= CRAMER_FLIP_MIN_TRADES and a multi-day streak
  * _topstep_flatten_all cancels resting protective stops before closing
  * new MLL peak is persisted immediately (survives restart)
"""
from __future__ import annotations

import base64
import json

import day_learner
from day_learner import (
    CRAMER_FLIP_MIN_DAYS,
    CRAMER_FLIP_MIN_TRADES,
    DayAdapt,
    DayStats,
    _build_adapt,
    apply_to_config,
)
from state import Position, State


# ── dashboard control-endpoint gate ───────────────────────────────────────────
def _handler(monkeypatch, user="", pw="", auth_header=""):
    import dashboard

    monkeypatch.setenv("DASH_USER", user)
    monkeypatch.setenv("DASH_PASS", pw)
    h = dashboard.Handler.__new__(dashboard.Handler)
    h.headers = {"Authorization": auth_header} if auth_header else {}
    return h


def test_control_denied_when_auth_unconfigured(monkeypatch):
    # No DASH_USER/DASH_PASS → read endpoints stay open but control must deny.
    h = _handler(monkeypatch)
    assert h._auth_ok()            # read-only surface unchanged
    assert not h._control_ok()     # stop/restart refuse to work


def test_control_denied_with_wrong_password(monkeypatch):
    bad = "Basic " + base64.b64encode(b"jackson:wrong").decode()
    h = _handler(monkeypatch, user="jackson", pw="right", auth_header=bad)
    assert not h._control_ok()


def test_control_allowed_with_correct_credentials(monkeypatch):
    good = "Basic " + base64.b64encode(b"jackson:right").decode()
    h = _handler(monkeypatch, user="jackson", pw="right", auth_header=good)
    assert h._control_ok()


# ── day_learner must never lower an operator halt ─────────────────────────────
class _Cfg:
    confidence_threshold = 1.01
    cramer_flip_threshold_usd = 1000.0


def test_day_learner_never_lowers_halt_threshold():
    cfg = _Cfg()
    adapt = DayAdapt(date="2026-07-05", confidence_threshold_adj=-0.05,
                     reasoning=["losing day → lower threshold"])
    apply_to_config(cfg, adapt)
    assert cfg.confidence_threshold == 1.01   # halt pinned, not clamped to 0.90


def test_day_learner_still_adjusts_normal_threshold():
    cfg = _Cfg()
    cfg.confidence_threshold = 0.75
    adapt = DayAdapt(date="2026-07-05", confidence_threshold_adj=+0.05,
                     reasoning=["choppy day → raise threshold"])
    apply_to_config(cfg, adapt)
    assert cfg.confidence_threshold == 0.80


# ── cramer flip guards ─────────────────────────────────────────────────────────
def _flip_stats(n_trades: int) -> DayStats:
    s = DayStats(date="2026-07-05", bot="topstep")
    s.n_trades = n_trades
    s.n_wins = 0
    s.total_pnl = -800.0
    s.shadow_pnl = 900.0          # edge = 1700 > full threshold (1000)
    return s


def _write_adapt_file(tmp_path, monkeypatch, streak: int):
    p = tmp_path / "day_adapt.json"
    p.write_text(json.dumps({"date": "2026-07-04", "cramer_flip_streak": streak}))
    monkeypatch.setattr(day_learner, "ADAPT_PATH", p)
    return p


def test_cramer_flip_rejected_below_min_trades(tmp_path, monkeypatch):
    _write_adapt_file(tmp_path, monkeypatch, streak=CRAMER_FLIP_MIN_DAYS)
    adapt = _build_adapt(_flip_stats(CRAMER_FLIP_MIN_TRADES - 1), None, _Cfg())
    assert not adapt.cramer_flip_enabled
    assert adapt.cramer_flip_streak == 0      # non-qualifying day resets streak


def test_cramer_flip_requires_consecutive_days(tmp_path, monkeypatch):
    _write_adapt_file(tmp_path, monkeypatch, streak=0)
    adapt = _build_adapt(_flip_stats(CRAMER_FLIP_MIN_TRADES), None, _Cfg())
    assert not adapt.cramer_flip_enabled      # day 1 of 3 — not armed
    assert adapt.cramer_flip_streak == 1


def test_cramer_flip_arms_after_streak(tmp_path, monkeypatch):
    _write_adapt_file(tmp_path, monkeypatch, streak=CRAMER_FLIP_MIN_DAYS - 1)
    adapt = _build_adapt(_flip_stats(CRAMER_FLIP_MIN_TRADES), None, _Cfg())
    assert adapt.cramer_flip_enabled
    assert adapt.cramer_flip_streak == CRAMER_FLIP_MIN_DAYS


# ── flatten cancels resting protective stops ──────────────────────────────────
def _pos(symbol: str, side: str, qty: float, entry: float, stop_oid: str) -> Position:
    p = Position(
        symbol=symbol, asset="future", side=side, qty=qty, entry_price=entry,
        size_usd=entry * qty, stop=0.0, target=0.0, kind="test", thesis="",
        opened_at="2026-07-03T12:00:00+00:00", mode="paper",
    )
    p.protective_order_id = stop_oid
    return p


def test_topstep_flatten_cancels_resting_stops(monkeypatch):
    import dataclasses

    import engine as eng
    import projectx_executor as px

    monkeypatch.setattr(
        px, "CONFIG",
        dataclasses.replace(px.CONFIG, projectx_username="", projectx_api_key=""))
    # Silence the notifier: test output must not leak into the live dashboard feed.
    monkeypatch.setattr(eng, "notify", lambda *a, **k: None)
    broker = px.ProjectXBroker()
    assert broker._mock_mode, "test must never talk to the live gateway"

    cancelled: list[str] = []
    monkeypatch.setattr(broker, "cancel_order", lambda oid: cancelled.append(oid) or True)
    monkeypatch.setattr(broker, "flatten_all", lambda: None)

    class _Exec:
        pass

    class _Topstep:
        def record_close(self, pnl, hold_s):
            pass

    e = eng.Engine.__new__(eng.Engine)
    e.executor = _Exec()
    e.executor.broker = broker
    e._topstep = _Topstep()
    e.state = State()
    e.state.save = lambda: None
    e._mark = lambda sym: 5000.0
    e.state.add(_pos("MNQ", "BUY", 1, 5000.0, "stop-111"))
    e.state.add(_pos("MES", "SELL", 2, 5000.0, "stop-222"))

    e._topstep_flatten_all("test breach")

    assert sorted(cancelled) == ["stop-111", "stop-222"]   # stops cancelled…
    assert e.state.open_positions == []                    # …and book flattened


# ── MLL peak persisted on every new high ──────────────────────────────────────
def test_new_peak_persists_to_day_state(tmp_path, monkeypatch):
    from topstep_risk import TopstepRiskManager

    m = TopstepRiskManager()
    monkeypatch.setattr(m, "_DAY_STATE_FILE", tmp_path / "day_state.json")
    m.reset_day(50_000.0)
    m.update_equity(50_400.0)                      # new high
    m.save_day_state("2026-07-05", False)          # what the engine now does

    m2 = TopstepRiskManager()
    monkeypatch.setattr(m2, "_DAY_STATE_FILE", tmp_path / "day_state.json")
    m2.reset_day(50_000.0)
    m2.load_day_state("2026-07-05")
    assert m2.peak_equity == 50_400.0              # restart restores the HIGH peak
