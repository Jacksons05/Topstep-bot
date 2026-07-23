"""Engine._bar_mark: the overnight entry/exit mark with a ProjectX bar-close
fallback. Guards the 2026-07-22 miss, where the live order-flow feed was still
reconnecting at the 18:00 ET reopen so _mark() returned None and the overnight
entry skipped every scan. _bar_mark must fall back to the last historical bar
close (a real futures price) while still preferring the live feed when present."""
from __future__ import annotations

from types import SimpleNamespace

from engine import Engine


def _fake(live, closes):
    class _Broker:
        def historical_bars(self, symbol, timeframe="5Min", limit=200):
            self.called_with = (symbol, timeframe, limit)
            return {"close": list(closes)} if closes is not None else {}
    return SimpleNamespace(_mark=lambda s: live,
                           executor=SimpleNamespace(broker=_Broker()))


def test_prefers_live_feed_when_available():
    # Live mark present -> use it, never touch history.
    f = _fake(live=17501.25, closes=[1.0, 2.0])
    assert Engine._bar_mark(f, "MNQ") == 17501.25


def test_falls_back_to_last_bar_close_when_live_none():
    # Reopen case: no live mark -> newest bar close (partial bar = current px).
    f = _fake(live=None, closes=[17490.0, 17495.5, 17502.75])
    assert Engine._bar_mark(f, "MNQ") == 17502.75
    assert f.executor.broker.called_with[0] == "MNQ"


def test_none_when_neither_source_has_data():
    # Feed cold AND history empty -> None, so callers skip (never assume 0).
    f = _fake(live=None, closes=None)
    assert Engine._bar_mark(f, "MNQ") is None


def test_ignores_nonpositive_live_and_bar():
    # A zero/negative live mark is not trusted; fall through to history.
    f = _fake(live=0.0, closes=[17400.0])
    assert Engine._bar_mark(f, "MNQ") == 17400.0
    # And a non-positive last bar close is rejected too.
    f2 = _fake(live=None, closes=[0.0])
    assert Engine._bar_mark(f2, "MNQ") is None


def test_no_broker_returns_none():
    f = SimpleNamespace(_mark=lambda s: None,
                        executor=SimpleNamespace(broker=None))
    assert Engine._bar_mark(f, "MNQ") is None
