"""Engineering-batch tests (2026-07-06): order idempotency, CME holiday
calendar, restart stop revalidation, contract rollover, SignalR self-heal."""
from __future__ import annotations

import dataclasses
import time as _time
from datetime import date, datetime, time, timedelta

import httpx
import pytest

import projectx_executor as px
from projectx_executor import OrderStateUnknown
from state import Position, State


def _mock_broker(monkeypatch) -> "px.ProjectXBroker":
    monkeypatch.setattr(
        px, "CONFIG",
        dataclasses.replace(px.CONFIG, projectx_username="", projectx_api_key=""))
    b = px.ProjectXBroker()
    assert b._mock_mode, "test must never talk to the live gateway"
    return b


class _FakeHTTP:
    """Scripted responses: each entry is an Exception to raise or a payload."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.headers = {}

    def post(self, path, json=None):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step

        class _R:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return step
        return _R()


def _wire(broker, monkeypatch, script):
    fake = _FakeHTTP(script)
    broker._http = fake
    broker.token = "tok"
    monkeypatch.setattr(broker, "_ensure_token", lambda: None)
    monkeypatch.setattr(px.time, "sleep", lambda s: None)
    return fake


# ── 1. order idempotency ──────────────────────────────────────────────────────
def test_mutating_post_raises_on_ambiguous_transport_error(monkeypatch):
    b = _mock_broker(monkeypatch)
    fake = _wire(b, monkeypatch, [httpx.ReadTimeout("read timed out")])
    with pytest.raises(OrderStateUnknown):
        b._post("/api/Order/place", {"x": 1}, mutating=True)
    assert fake.calls == 1        # exactly one attempt — never re-POSTed


def test_mutating_post_retries_connect_phase_errors(monkeypatch):
    b = _mock_broker(monkeypatch)
    fake = _wire(b, monkeypatch,
                 [httpx.ConnectError("refused"), {"success": True, "orderId": 7}])
    d = b._post("/api/Order/place", {"x": 1}, mutating=True)
    assert d["orderId"] == 7
    assert fake.calls == 2        # connect error = request never sent = safe retry


def test_non_mutating_post_still_retries_read_errors(monkeypatch):
    b = _mock_broker(monkeypatch)
    fake = _wire(b, monkeypatch, [httpx.ReadError("reset"), {"ok": True}])
    d = b._post("/api/Trade/search", {"x": 1})
    assert d == {"ok": True}
    assert fake.calls == 2


# ── 2. CME holiday / early-close calendar ─────────────────────────────────────
def test_calendar_classifies_2026_days():
    import cme_calendar as cal
    assert cal.is_full_closure(date(2026, 12, 25))          # Christmas
    assert cal.early_close_halt(date(2026, 11, 26)) == time(13, 0)   # Thanksgiving
    assert cal.early_close_halt(date(2026, 7, 8)) is None   # normal Wednesday
    assert cal.year_covered(2026) and cal.year_covered(2027)
    assert not cal.year_covered(2031)                       # forces the annual update


def _freeze_engine_now(monkeypatch, dt_et):
    import engine as eng
    from zoneinfo import ZoneInfo

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            base = dt_et.replace(tzinfo=ZoneInfo("America/New_York"))
            return base.astimezone(tz) if tz else base
    monkeypatch.setattr(eng, "datetime", _DT)


def _hours_engine():
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    return e


def test_market_closed_on_full_holiday(monkeypatch):
    _freeze_engine_now(monkeypatch, datetime(2026, 12, 25, 14, 0))   # Christmas, Friday
    assert not _hours_engine()._market_open()


def test_market_closed_after_early_close_halt(monkeypatch):
    _freeze_engine_now(monkeypatch, datetime(2026, 11, 26, 14, 0))   # Thanksgiving 14:00
    assert not _hours_engine()._market_open()


def test_market_open_before_early_close_halt(monkeypatch):
    _freeze_engine_now(monkeypatch, datetime(2026, 11, 26, 10, 0))   # Thanksgiving 10:00
    assert _hours_engine()._market_open()


def test_market_closed_friday_evening(monkeypatch):
    # Regression for the weekend-start bug: Friday 19:00 ET must be CLOSED
    # (weekend runs Fri 17:00 → Sun 18:00; the old rule only blocked 17-18h).
    _freeze_engine_now(monkeypatch, datetime(2026, 7, 10, 19, 0))    # normal Friday
    assert not _hours_engine()._market_open()


def test_flatten_pulls_forward_on_early_close(monkeypatch):
    import topstep_risk as tr
    m = tr.TopstepRiskManager()

    class _DT(datetime):
        _fixed: datetime

        @classmethod
        def now(cls, tz=None):
            return cls._fixed.replace(tzinfo=tz) if tz else cls._fixed

    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    # Thanksgiving 2026, 12:50 ET — halt 13:00, margin 15min → window opened 12:45.
    monkeypatch.setattr(tr, "_now_et",
                        lambda: datetime(2026, 11, 26, 12, 50, tzinfo=et))
    assert m.should_flatten_now()
    # 11:00 ET same day — before the pulled-forward window.
    monkeypatch.setattr(tr, "_now_et",
                        lambda: datetime(2026, 11, 26, 11, 0, tzinfo=et))
    assert not m.should_flatten_now()


# ── 3. restart stop revalidation ──────────────────────────────────────────────
def _pos(symbol, side, qty, entry, stop=0.0, pid=""):
    p = Position(
        symbol=symbol, asset="future", side=side, qty=qty, entry_price=entry,
        size_usd=entry * qty, stop=stop, target=0.0, kind="test", thesis="",
        opened_at="2026-07-03T12:00:00+00:00", mode="paper",
    )
    p.protective_order_id = pid
    return p


def test_restart_revalidation_rearms_stops(monkeypatch):
    import engine as eng
    monkeypatch.setattr(eng, "notify", lambda *a, **k: None)
    b = _mock_broker(monkeypatch)

    cancelled, placed = [], []
    monkeypatch.setattr(b, "cancel_order", lambda oid: cancelled.append(oid) or True)
    monkeypatch.setattr(b, "place_stop_order",
                        lambda sym, qty, side, px_: placed.append((sym, qty, side, px_)) or "new-stop-1")

    class _Exec:
        pass
    e = eng.Engine.__new__(eng.Engine)
    e.executor = _Exec()
    e.executor.broker = b
    e.state = State()
    e.state.save = lambda: None
    monkeypatch.setattr(e, "_live_projectx", lambda: True)

    long_pos = _pos("MNQ", "BUY", 2, 5000.0, stop=4950.0, pid="stale-stop-9")
    naked = _pos("MES", "SELL", 1, 5000.0, stop=0.0)      # no stop price → warn only
    e.state.add(long_pos)
    e.state.add(naked)

    e._revalidate_protective_stops()

    assert cancelled == ["stale-stop-9"]                  # stale id cancelled first
    assert placed == [("MNQ", 2, "SELL", 4950.0)]         # long → SELL stop, re-armed
    assert long_pos.protective_order_id == "new-stop-1"
    assert naked.protective_order_id == ""                # untouched — warned only


# ── 4. contract rollover awareness ────────────────────────────────────────────
def test_contract_cache_refreshes_daily_and_flags_roll(monkeypatch):
    b = _mock_broker(monkeypatch)
    b._mock_mode = False   # exercise the resolution path with a scripted _post
    responses = [
        {"contracts": [{"id": "CON.F.US.MNQ.H26", "activeContract": True}]},
        {"contracts": [{"id": "CON.F.US.MNQ.M26", "activeContract": True}]},
    ]
    monkeypatch.setattr(b, "_post", lambda path, body, **kw: responses.pop(0))

    assert b.contract_id("MNQ") == "CON.F.US.MNQ.H26"
    assert b.contract_id("MNQ") == "CON.F.US.MNQ.H26"     # same-day: cached, no call
    assert len(responses) == 1

    b._cid_resolved_on["MNQ"] = date.today() - timedelta(days=1)   # simulate next day
    assert b.contract_id("MNQ") == "CON.F.US.MNQ.M26"     # re-resolved → new month
    assert b._cid_to_root["CON.F.US.MNQ.M26"] == "MNQ"


def test_contract_cache_keeps_stale_on_api_failure(monkeypatch):
    b = _mock_broker(monkeypatch)
    b._mock_mode = False
    calls = iter([
        {"contracts": [{"id": "CID.A", "activeContract": True}]},
    ])
    monkeypatch.setattr(b, "_post", lambda path, body, **kw: next(calls))
    assert b.contract_id("MES") == "CID.A"
    b._cid_resolved_on["MES"] = date.today() - timedelta(days=1)
    monkeypatch.setattr(b, "_post",
                        lambda path, body, **kw: (_ for _ in ()).throw(RuntimeError("down")))
    assert b.contract_id("MES") == "CID.A"   # stale beats halting mid-session


# ── 5. SignalR staleness self-heal ────────────────────────────────────────────
def _feed(monkeypatch):
    from projectx_marketdata import ProjectXOrderFlowFeed

    class _MB:
        _mock_mode = True
        token = "tok"
        def _ensure_token(self):
            pass

    f = ProjectXOrderFlowFeed(_MB())
    return f


def test_heal_rebuilds_after_prolonged_silence(monkeypatch):
    f = _feed(monkeypatch)
    f._mock = False
    f._conn = object()
    f._subscribed = {"MNQ"}
    eng = f.get("MNQ")
    eng.last_quote_ts = _time.time() - 300   # 5 min silent, market open

    events = []
    monkeypatch.setattr(f, "close", lambda: events.append("close"))
    monkeypatch.setattr(f, "subscribe", lambda roots: events.append(("sub", tuple(roots))) or 1)

    f.heal_if_stale()
    assert events == ["close", ("sub", ("MNQ",))]

    # Immediately again → cooldown suppresses a second rebuild.
    f._conn = object()
    f._subscribed = {"MNQ"}
    f.heal_if_stale()
    assert events == ["close", ("sub", ("MNQ",))]


def test_heal_noop_when_feed_fresh(monkeypatch):
    f = _feed(monkeypatch)
    f._mock = False
    f._conn = object()
    f._subscribed = {"MNQ"}
    f.get("MNQ").last_quote_ts = _time.time() - 5   # fresh
    monkeypatch.setattr(f, "close", lambda: pytest.fail("must not rebuild a fresh feed"))
    f.heal_if_stale()


def test_heal_noop_immediately_after_reconnect(monkeypatch):
    """heal_if_stale must not rebuild a connection that just auto-reconnected.

    The re-subscribe following a reconnect may take a few seconds to start
    delivering ticks; treating that silence as a dead feed would immediately
    tear down and rebuild the healthy-but-quiet connection.

    Silence is deliberately well below _HEAL_HARD_S (300s), not exactly at
    it — at the boundary this test would race the hard-ceiling check below
    (wall-clock time elapsed between setting last_quote_ts and calling
    heal_if_stale pushes `now - freshest` slightly past 300, which reads as
    "hard ceiling reached" and defeats the very cooldown grace this test
    exists to verify). Keep this comfortably under the ceiling; the ceiling
    itself is covered by test_heal_forces_rebuild_past_hard_ceiling below."""
    f = _feed(monkeypatch)
    f._mock = False
    f._conn = object()
    f._subscribed = {"MNQ"}
    eng = f.get("MNQ")
    eng.last_quote_ts = _time.time() - 200   # long silence, but under HEAL_HARD_S
    f._last_reconnect_ts = _time.time() - 10  # reconnected 10s ago (< HEAL_COOLDOWN_S)
    monkeypatch.setattr(f, "close", lambda: pytest.fail("must not rebuild after a recent reconnect"))
    f.heal_if_stale()


def test_heal_forces_rebuild_past_hard_ceiling(monkeypatch):
    """Past _HEAL_HARD_S, a recent reconnect must no longer defer the heal.

    Models the reconnect-churn case _HEAL_HARD_S exists for: repeated
    socket-close/reconnect cycles keep stamping _last_reconnect_ts (always
    "recent"), which would otherwise defer heal_if_stale forever even though
    the feed has been silent far longer than any legitimate re-subscribe
    delay. Once total silence clears the hard ceiling, the rebuild must fire
    regardless of how recently the last (unproductive) reconnect happened."""
    f = _feed(monkeypatch)
    f._mock = False
    f._conn = object()
    f._subscribed = {"MNQ"}
    eng = f.get("MNQ")
    eng.last_quote_ts = _time.time() - (f._HEAL_HARD_S + 30)  # past the hard ceiling
    f._last_reconnect_ts = _time.time() - 10                   # still "recent"

    events = []
    monkeypatch.setattr(f, "close", lambda: events.append("close"))
    monkeypatch.setattr(f, "subscribe", lambda roots: events.append(("sub", tuple(roots))) or 1)

    f.heal_if_stale()
    assert events == ["close", ("sub", ("MNQ",))]


def test_heal_noop_when_no_engines(monkeypatch):
    """heal_if_stale must not raise when the engines dict is empty."""
    f = _feed(monkeypatch)
    f._mock = False
    f._conn = object()
    f._subscribed = {"MNQ"}
    # engines dict deliberately left empty (nothing subscribed yet)
    assert not f._engines
    monkeypatch.setattr(f, "close", lambda: pytest.fail("must not rebuild with no engines"))
    f.heal_if_stale()  # must be a silent no-op, not ValueError from max()
