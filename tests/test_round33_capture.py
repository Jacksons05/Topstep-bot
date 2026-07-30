"""Round 33 broad-universe IB-break capture: pure-logic tests, no network.
Covers the three functions that matter for correctness -- session-open
detection, IB computation, and first-break detection -- since the capture
script's entire value is in these facts being recorded accurately."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import round33_capture as r33

_ET = ZoneInfo("America/New_York")


def _bar(hh, mm, o, h, l, c, v, day=date(2026, 7, 27)):
    return {"ts": datetime(day.year, day.month, day.day, hh, mm, tzinfo=_ET),
            "open": o, "high": h, "low": l, "close": c, "volume": v}


# ── detect_session_open ────────────────────────────────────────────────────
def test_detect_session_open_finds_the_volume_step():
    bars = (
        [_bar(6, m, 100, 100.1, 99.9, 100, 5) for m in range(0, 55, 5)]  # quiet overnight
        + [_bar(9, 30, 100, 100.5, 99.8, 100.2, 400),   # volume steps up here
           _bar(9, 35, 100.2, 100.6, 100.0, 100.4, 380),
           _bar(9, 40, 100.4, 100.8, 100.2, 100.6, 350)]
    )
    open_ts = r33.detect_session_open(bars)
    assert open_ts is not None
    assert open_ts.hour == 9 and open_ts.minute == 30


def test_detect_session_open_none_when_no_clear_step():
    bars = [_bar(h, 0, 100, 100.1, 99.9, 100, 10) for h in range(0, 20)]  # flat volume all day
    assert r33.detect_session_open(bars) is None


def test_detect_session_open_none_on_too_few_bars():
    assert r33.detect_session_open([_bar(9, 30, 100, 100, 100, 100, 500)]) is None


# ── initial_balance ─────────────────────────────────────────────────────────
def test_initial_balance_30min_window():
    open_ts = datetime(2026, 7, 27, 9, 30, tzinfo=_ET)
    bars = [
        _bar(9, 30, 100, 101, 99.5, 100.5, 100),
        _bar(9, 35, 100.5, 102, 100.0, 101, 100),   # highest high
        _bar(9, 55, 101, 101.5, 98.0, 99, 100),      # lowest low, still inside 30min
        _bar(10, 5, 99, 105, 95, 100, 100),          # OUTSIDE the 30min window -- must be excluded
    ]
    result = r33.initial_balance(bars, open_ts, 30)
    assert result is not None
    ibh, ibl, window_end = result
    assert ibh == 102       # from the 9:35 bar, not the 10:05 outlier
    assert ibl == 98.0      # from the 9:55 bar
    assert window_end == open_ts + timedelta(minutes=30)


def test_initial_balance_none_when_no_bars_in_window():
    open_ts = datetime(2026, 7, 27, 9, 30, tzinfo=_ET)
    bars = [_bar(11, 0, 100, 100, 100, 100, 50)]  # nowhere near the window
    assert r33.initial_balance(bars, open_ts, 30) is None


# ── first_break ──────────────────────────────────────────────────────────────
def test_first_break_detects_upside_break_with_next_bar_open_as_entry():
    window_end = datetime(2026, 7, 27, 10, 0, tzinfo=_ET)
    bars = [
        _bar(10, 0, 100, 100.5, 99.5, 100.2, 50),    # inside IB, no break
        _bar(10, 5, 100.2, 103, 100.0, 102.5, 80),   # closes above IBH=101 -> break
        _bar(10, 10, 102.5, 103, 102, 102.8, 60),    # entry price comes from HERE (next bar open)
    ]
    side, btime, bprice, entry = r33.first_break(bars, window_end, ibh=101.0, ibl=98.0)
    assert side == "up"
    assert btime.minute == 5
    assert bprice == 102.5
    assert entry == 102.5  # next bar's OPEN, not its close -- no lookahead


def test_first_break_detects_downside_break():
    window_end = datetime(2026, 7, 27, 10, 0, tzinfo=_ET)
    bars = [_bar(10, 5, 100, 100.2, 96, 97.0, 80), _bar(10, 10, 97.0, 97.5, 96.5, 97.2, 60)]
    side, btime, bprice, entry = r33.first_break(bars, window_end, ibh=101.0, ibl=98.0)
    assert side == "down"
    assert entry == 97.0


def test_first_break_none_when_price_stays_inside_ib():
    window_end = datetime(2026, 7, 27, 10, 0, tzinfo=_ET)
    bars = [_bar(10, 5, 100, 100.5, 99.5, 100.2, 50)]
    side, btime, bprice, entry = r33.first_break(bars, window_end, ibh=101.0, ibl=98.0)
    assert side is None and btime is None and bprice is None and entry is None


def test_first_break_returns_none_entry_when_break_is_the_last_bar_so_far():
    """A break with no bar after it yet: side/time/price are still recorded
    (facts as observed), but entry must be None -- there's no next-bar-open
    to enter at today, and the capture row must reflect that rather than
    fabricate an entry price."""
    window_end = datetime(2026, 7, 27, 10, 0, tzinfo=_ET)
    bars = [_bar(10, 5, 100, 103, 99.5, 102.5, 80)]
    side, btime, bprice, entry = r33.first_break(bars, window_end, ibh=101.0, ibl=98.0)
    assert side == "up" and bprice == 102.5
    assert entry is None


# ── UNIVERSE sanity ──────────────────────────────────────────────────────────
def test_universe_has_33_unique_symbols():
    assert len(r33.UNIVERSE) == 33
    assert len(set(r33.UNIVERSE)) == 33  # no accidental duplicates


# ── run_capture: broker-injected entry point (used by engine._maybe_run_round33_capture) ──
def test_run_capture_uses_the_injected_broker_not_its_own(monkeypatch, tmp_path):
    """run_capture() must work off whatever broker it's handed -- the whole
    point of moving this into the engine is reusing ITS connection, not
    building a new one."""
    calls = []

    class _StubBroker:
        def historical_bars(self, symbol, timeframe="5Min", limit=200, **kw):
            calls.append(symbol)
            return {}  # empty -> "no bars" rows, but proves each symbol was hit

        def contract_id(self, symbol):
            return None

    monkeypatch.setattr(r33, "CSV_PATH", tmp_path / "capture.csv")
    summary = r33.run_capture(_StubBroker(), today=date(2026, 7, 29))
    assert set(calls) == set(r33.UNIVERSE)
    assert "2026-07-29" in summary
    assert (tmp_path / "capture.csv").exists()


# ── already_captured: durable (file-based) once-per-day guard ─────────────
def test_already_captured_false_when_csv_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(r33, "CSV_PATH", tmp_path / "nope.csv")
    assert r33.already_captured(date(2026, 7, 29)) is False


def test_already_captured_true_after_a_real_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(r33, "CSV_PATH", tmp_path / "capture.csv")

    class _StubBroker:
        def historical_bars(self, *a, **kw):
            return {}

        def contract_id(self, symbol):
            return None

    r33.run_capture(_StubBroker(), today=date(2026, 7, 29))
    assert r33.already_captured(date(2026, 7, 29)) is True
    assert r33.already_captured(date(2026, 7, 30)) is False  # different day, untouched


def test_run_capture_is_a_noop_on_a_second_call_for_the_same_day(tmp_path, monkeypatch):
    """This is the actual bug this guard exists to prevent: a restart landing
    after 16:30 ET on a day already captured must not duplicate the rows."""
    monkeypatch.setattr(r33, "CSV_PATH", tmp_path / "capture.csv")
    calls = []

    class _StubBroker:
        def historical_bars(self, symbol, *a, **kw):
            calls.append(symbol)
            return {}

        def contract_id(self, symbol):
            return None

    r33.run_capture(_StubBroker(), today=date(2026, 7, 29))
    first_call_count = len(calls)
    assert first_call_count == len(r33.UNIVERSE)

    summary = r33.run_capture(_StubBroker(), today=date(2026, 7, 29))
    assert len(calls) == first_call_count, "second call must not touch the broker at all"
    assert "already captured" in summary


# ── engine._maybe_run_round33_capture: once-per-session, time-gated, no second connection ──
def _bare_engine():
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    e._round33_last_capture_date = None
    return e


class _LiveStubBroker:
    """Looks like a real (non-mock) ProjectXBroker to the gate checks."""
    _mock_mode = False

    def historical_bars(self, symbol, timeframe="5Min", limit=200, **kw):
        return {}


def test_round33_capture_skipped_before_1630_et(monkeypatch):
    import engine as eng
    import round33_capture as r33mod

    e = _bare_engine()
    e.executor = type("E", (), {"broker": _LiveStubBroker()})()
    calls = []
    monkeypatch.setattr(r33mod, "run_capture", lambda broker, today=None: calls.append(today))
    monkeypatch.setattr(eng, "datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: datetime(2026, 7, 29, 16, 0, tzinfo=r33mod._ET))}))

    e._maybe_run_round33_capture()
    assert calls == []
    assert e._round33_last_capture_date is None


def test_round33_capture_runs_once_after_1630_et_then_skips_same_day(monkeypatch):
    import engine as eng
    import round33_capture as r33mod

    e = _bare_engine()
    e.executor = type("E", (), {"broker": _LiveStubBroker()})()
    calls = []
    monkeypatch.setattr(r33mod, "run_capture",
                         lambda broker, today=None: calls.append(today) or "summary")
    monkeypatch.setattr(eng, "datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: datetime(2026, 7, 29, 16, 45, tzinfo=r33mod._ET))}))

    e._maybe_run_round33_capture()
    assert calls == [date(2026, 7, 29)]
    assert e._round33_last_capture_date == date(2026, 7, 29)

    e._maybe_run_round33_capture()  # same day, later cycle -- must NOT re-run
    assert calls == [date(2026, 7, 29)]


def test_round33_capture_uses_plain_calendar_date_not_session_date(monkeypatch):
    """Regression for the 2026-07-29 bug: a late-evening check (well past
    18:00 ET, when _topstep_session_date() has already rolled to tomorrow)
    must still tag the capture with TODAY's calendar date -- capture_symbol
    filters bars by the actual date they printed on, not a rolled-forward
    session label. Using the session date here mislabeled every row as
    tomorrow and every symbol came back 'no bars' (looking for a day that
    hadn't started yet)."""
    import engine as eng
    import round33_capture as r33mod

    e = _bare_engine()
    e.executor = type("E", (), {"broker": _LiveStubBroker()})()
    # A session-date getter that would (wrongly, if it were consulted) roll
    # forward -- asserts it's never even called by this method anymore.
    e._topstep_session_date = lambda: (_ for _ in ()).throw(
        AssertionError("must not consult session date -- use the plain calendar date"))
    calls = []
    monkeypatch.setattr(r33mod, "run_capture",
                         lambda broker, today=None: calls.append(today) or "summary")
    # 22:33 ET, well past the 18:00 session roll -- the actual bug's timing.
    monkeypatch.setattr(eng, "datetime", type("D", (), {
        "now": staticmethod(lambda tz=None: datetime(2026, 7, 29, 22, 33, tzinfo=r33mod._ET))}))

    e._maybe_run_round33_capture()
    assert calls == [date(2026, 7, 29)], "must capture TODAY's date, not tomorrow's session label"


def test_round33_capture_skipped_when_broker_has_no_live_connection():
    """Sim/paper fallback or mock-mode broker -- must not attempt a capture
    (there's nothing real to capture, and no reason to touch round33_capture
    at all)."""
    e = _bare_engine()

    class _MockBroker:
        _mock_mode = True

        def historical_bars(self, *a, **kw):
            raise AssertionError("must not be called in mock mode")

    e.executor = type("E", (), {"broker": _MockBroker()})()
    e._maybe_run_round33_capture()  # must not raise, must not set the guard
    assert e._round33_last_capture_date is None


def test_round33_capture_skipped_when_broker_lacks_historical_bars():
    """A broker type with no historical_bars at all (e.g. the base Sim
    executor) -- must not crash on the hasattr check."""
    e = _bare_engine()
    e.executor = type("E", (), {"broker": object()})()
    e._maybe_run_round33_capture()
    assert e._round33_last_capture_date is None
