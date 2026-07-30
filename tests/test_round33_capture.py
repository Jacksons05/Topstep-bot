"""Round 33 broad-universe IB-break capture: pure-logic tests, no network.
Covers the three functions that matter for correctness -- session-open
detection, IB computation, and first-break detection -- since the capture
script's entire value is in these facts being recorded accurately."""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "oos"))
import round33_broad_ib_capture as r33  # noqa: E402

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
