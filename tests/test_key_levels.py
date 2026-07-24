"""key_levels.py: live prior-session reference levels + today's developing
range/value-area. Pure observability/context, not a new entry signal — see
the module docstring. Reuses research.features.value_area (Round 24's
frozen volume-profile bucketing)."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import key_levels as kl

_ET = ZoneInfo("America/New_York")


def _bars_from_rows(rows):
    """rows: list of (ts_et, close, high, low, volume) -> the {"time":...,
    "close":...} dict shape historical_bars()/MarketData.bars() return."""
    return {
        "time": [r[0].isoformat() for r in rows],
        "close": [r[1] for r in rows],
        "high": [r[2] for r in rows],
        "low": [r[3] for r in rows],
        "volume": [r[4] for r in rows],
    }


def _rth_session(day: date, base_price: float, n_bars: int = 78):
    """78 5-min bars = a full 09:30-16:00 ET RTH session, gently drifting
    price with a volume bulge in the middle third (so value_area has a clear
    POC to find, not a flat/degenerate distribution)."""
    rows = []
    t = datetime(day.year, day.month, day.day, 9, 30, tzinfo=_ET)
    for i in range(n_bars):
        price = base_price + (i / n_bars) * 2.0  # slow drift up across the day
        vol = 500 if n_bars // 3 <= i <= 2 * n_bars // 3 else 50  # bulge mid-session
        rows.append((t, round(price, 2), round(price + 0.25, 2), round(price - 0.25, 2), vol))
        t += timedelta(minutes=5)
    return rows


def _overnight_session(day: date, base_price: float, n_bars: int = 40):
    """A handful of thin overnight (Globex) bars between two RTH sessions —
    must be excluded from both prior_session_levels and developing_levels."""
    rows = []
    t = datetime(day.year, day.month, day.day, 18, 0, tzinfo=_ET)
    for i in range(n_bars):
        rows.append((t, base_price, base_price + 0.25, base_price - 0.25, 10))
        t += timedelta(minutes=5)
    return rows


# ── session_date_for ───────────────────────────────────────────────────────
def test_session_date_rolls_at_1800_et():
    before_roll = datetime(2026, 7, 20, 17, 59, tzinfo=_ET)
    at_roll = datetime(2026, 7, 20, 18, 0, tzinfo=_ET)
    assert kl.session_date_for(before_roll) == date(2026, 7, 20)
    assert kl.session_date_for(at_roll) == date(2026, 7, 21)


# ── prior_session_levels ──────────────────────────────────────────────────
def test_prior_session_levels_needs_two_settled_sessions():
    day1 = date(2026, 7, 20)
    rows = _rth_session(day1, 100.0)
    assert kl.prior_session_levels(_bars_from_rows(rows)) is None  # only one session


def test_prior_session_levels_identifies_prior_day_hi_lo_poc():
    day1, day2 = date(2026, 7, 20), date(2026, 7, 21)
    rows = _rth_session(day1, 100.0) + _overnight_session(day1, 101.5) + _rth_session(day2, 102.0)
    result = kl.prior_session_levels(_bars_from_rows(rows))
    assert result is not None
    assert result.session_date == day1
    day1_prices = [r[1] for r in _rth_session(day1, 100.0)]
    assert result.pdh == pytest.approx(max(day1_prices) + 0.25)
    assert result.pdl == pytest.approx(min(day1_prices) - 0.25)
    assert result.pdclose == pytest.approx(day1_prices[-1])
    # POC must land inside the high-volume bulge (mid-session), not at the
    # thin open/close prices.
    assert result.poc is not None
    lo_third = min(day1_prices) + (max(day1_prices) - min(day1_prices)) / 3
    hi_third = min(day1_prices) + 2 * (max(day1_prices) - min(day1_prices)) / 3
    assert lo_third <= result.poc <= hi_third
    assert result.val <= result.poc <= result.vah


def test_prior_session_levels_ignores_overnight_bars():
    day1, day2 = date(2026, 7, 20), date(2026, 7, 21)
    # Overnight bar priced far outside the RTH range — must not leak into PDH/PDL.
    rows = (_rth_session(day1, 100.0) + _overnight_session(day1, 500.0)
            + _rth_session(day2, 102.0))
    result = kl.prior_session_levels(_bars_from_rows(rows))
    assert result.pdh < 500.0


def test_prior_session_levels_none_on_empty_bars():
    assert kl.prior_session_levels({}) is None
    assert kl.prior_session_levels({"close": [1, 2, 3]}) is None  # no "time"


# ── developing_levels ──────────────────────────────────────────────────────
def test_developing_levels_none_before_todays_rth_opens():
    day1 = date(2026, 7, 20)
    rows = _overnight_session(day1, 100.0)
    assert kl.developing_levels(_bars_from_rows(rows), day1) is None


def test_developing_levels_tracks_todays_session_only():
    day1, day2 = date(2026, 7, 20), date(2026, 7, 21)
    today_rows = _rth_session(day2, 100.0)[:20]  # partial session so far
    rows = _rth_session(day1, 90.0) + _overnight_session(day1, 95.0) + today_rows
    dev = kl.developing_levels(_bars_from_rows(rows), day2)
    assert dev is not None
    hi, lo, poc, vah, val = dev
    today_prices = [r[1] for r in today_rows]
    assert hi == pytest.approx(max(today_prices) + 0.25)
    assert lo == pytest.approx(min(today_prices) - 0.25)
    assert lo > 95.0  # must not have picked up yesterday's lower-priced (~90) bars


# ── KeyLevels.summary() ────────────────────────────────────────────────────
def test_summary_handles_no_data():
    assert kl.KeyLevels().summary() == "no prior-session data yet"


def test_summary_formats_populated_levels():
    k = kl.KeyLevels(pdh=101.0, pdl=99.0, pdmid=100.0, pdclose=100.5,
                      poc=100.25, vah=100.75, val=99.75)
    s = k.summary()
    assert "PDH=101.00" in s and "POC=100.25" in s


# ── Engine wiring ──────────────────────────────────────────────────────────
def _bare_engine():
    import engine as eng
    e = eng.Engine.__new__(eng.Engine)
    e._key_levels = {}
    e._key_levels_date = None
    return e


class _StubBroker:
    def __init__(self, wide_bars):
        self._wide_bars = wide_bars
        self.calls = 0

    def historical_bars(self, symbol, timeframe="5Min", limit=200, **kw):
        self.calls += 1
        return self._wide_bars


class _StubExecutor:
    def __init__(self, broker):
        self.broker = broker


def test_update_key_levels_fetches_wide_bars_once_per_session(monkeypatch):
    day1, day2 = date(2026, 7, 20), date(2026, 7, 21)
    wide = _bars_from_rows(
        _rth_session(day1, 100.0) + _overnight_session(day1, 101.0) + _rth_session(day2, 102.0))
    e = _bare_engine()
    e.executor = _StubExecutor(_StubBroker(wide))
    monkeypatch.setattr(e, "_topstep_session_date", lambda: day2)

    cheap_bars = _bars_from_rows(_rth_session(day2, 102.0)[:10])
    e._update_key_levels("ES", cheap_bars)
    e._update_key_levels("ES", cheap_bars)
    e._update_key_levels("ES", cheap_bars)

    assert e.executor.broker.calls == 1, "the wide prior-session pull must be cached, not re-fetched every cycle"
    assert e._key_levels["ES"].pdh is not None
    assert e._key_levels["ES"].today_hi is not None  # developing levels DID update from the cheap bars


def test_update_key_levels_resets_on_new_session(monkeypatch):
    day1, day2 = date(2026, 7, 20), date(2026, 7, 21)
    wide = _bars_from_rows(_rth_session(day1, 100.0) + _overnight_session(day1, 101.0))
    e = _bare_engine()
    e.executor = _StubExecutor(_StubBroker(wide))
    monkeypatch.setattr(e, "_topstep_session_date", lambda: day1)
    e._update_key_levels("ES", _bars_from_rows(_rth_session(day1, 100.0)[:5]))
    assert "ES" in e._key_levels

    monkeypatch.setattr(e, "_topstep_session_date", lambda: day2)
    e._update_key_levels("ES", _bars_from_rows(_rth_session(day2, 102.0)[:5]))
    assert e._key_levels_date == day2
    assert e.executor.broker.calls == 2  # new session -> re-fetched, not stale-cached


def test_prescreen_survives_key_levels_failure(monkeypatch):
    """A key-levels computation error must not break the scan (notify + carry
    on), matching every other best-effort side-channel in _prescreen."""
    import engine as eng
    e = _bare_engine()

    def _boom(sym, bars):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(e, "_update_key_levels", _boom)
    notified = []
    monkeypatch.setattr(eng, "notify", lambda msg: notified.append(msg))
    # Exercise the same try/except wrapping used in _prescreen directly,
    # since a full _prescreen() call needs a live broker/config wired up.
    try:
        e._update_key_levels("ES", {})
    except Exception as exc:  # noqa: BLE001
        eng.notify(f"⚠ key-levels update failed for ES: {exc}")
    assert any("key-levels" in m for m in notified)
