"""Overnight-drift decision-module tests: entry/exit windows, loss-streak halt,
once-per-session guard, session roll, stop sizing. (Supersedes the old standalone
16:00->09:30 runner tests; that window violated the flatten rule — the evening
18:00->06:00 slice does not.)"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import overnight_drift as od

ET = ZoneInfo("America/New_York")


def _cfg(**kw):
    base = dict(overnight_drift_symbol="MNQ", overnight_drift_entry_et="18:00",
                overnight_drift_exit_et="06:00", overnight_drift_entry_window_min=60,
                overnight_drift_stop_usd=500.0, overnight_drift_max_losing_nights=2,
                overnight_drift_contracts=1)
    base.update(kw)
    return SimpleNamespace(**base)


def _strat(tmp_path, **kw):
    od.STATE_PATH = tmp_path / "od.json"          # isolate persistent state
    return od.OvernightDrift(_cfg(**kw))


def _dt(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET)


def test_enter_in_window_when_flat(tmp_path):
    s = _strat(tmp_path)
    assert s.should_enter(_dt(2026, 7, 21, 18, 5), is_flat=True)[0]   # Tue 18:05


def test_no_enter_outside_window(tmp_path):
    s = _strat(tmp_path)
    assert not s.should_enter(_dt(2026, 7, 21, 20, 0), is_flat=True)[0]  # past 18:00+60
    assert not s.should_enter(_dt(2026, 7, 21, 10, 0), is_flat=True)[0]


def test_no_enter_when_not_flat(tmp_path):
    s = _strat(tmp_path)
    assert not s.should_enter(_dt(2026, 7, 21, 18, 5), is_flat=False)[0]


def test_no_enter_saturday(tmp_path):
    s = _strat(tmp_path)
    assert not s.should_enter(_dt(2026, 7, 25, 18, 5), is_flat=True)[0]  # Saturday


def test_once_per_session(tmp_path):
    s = _strat(tmp_path)
    now = _dt(2026, 7, 21, 18, 5)
    assert s.should_enter(now, True)[0]
    s.mark_entered(now)
    assert not s.should_enter(_dt(2026, 7, 21, 18, 30), True)[0]        # same session


def test_loss_streak_halt(tmp_path):
    s = _strat(tmp_path)
    s.record_result(-500.0)
    assert not s.halted()
    s.record_result(-300.0)
    assert s.halted()
    assert not s.should_enter(_dt(2026, 7, 22, 18, 5), True)[0]
    s.record_result(+400.0)          # a win resets the streak
    assert not s.halted() and s.consecutive_losses == 0


def test_exit_at_or_after_0600(tmp_path):
    s = _strat(tmp_path)
    assert s.should_exit(_dt(2026, 7, 22, 6, 5), have_position=True)
    assert not s.should_exit(_dt(2026, 7, 22, 3, 0), have_position=True)   # before 06:00
    assert not s.should_exit(_dt(2026, 7, 22, 6, 5), have_position=False)


def test_session_roll_18et(tmp_path):
    s = _strat(tmp_path)
    assert str(s.session_date(_dt(2026, 7, 21, 18, 5))) == "2026-07-22"
    assert str(s.session_date(_dt(2026, 7, 21, 10, 0))) == "2026-07-21"


def test_stop_points(tmp_path):
    s = _strat(tmp_path)
    assert s.stop_points(2.0) == 250.0     # $500 / $2 per point (MNQ) = 250 pts
