"""Deterministic unit tests — no network, no API key.

Covers the indicator math, the event/macro fail-open guards, news sentiment,
the risk gate's circuit breaker, and ATR-bracket exits. (The options-exposure
tests live in the sister equity bot; this fork is futures-only.)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import NewsFeed, NewsItem  # noqa: E402
from risk import circuit_breaker, should_exit  # noqa: E402
from signals import atr, quant_signal, rsi, sma  # noqa: E402
from state import Position  # noqa: E402


# ── indicators ────────────────────────────────────────────

def test_sma_needs_enough_points():
    assert sma([1, 2], 5) is None
    assert sma([1, 2, 3, 4, 5], 5) == 3.0


def test_rsi_all_gains_is_100():
    closes = list(range(1, 30))  # strictly rising
    assert rsi(closes, 14) == 100.0


def test_atr_positive():
    highs = [10 + i for i in range(20)]
    lows = [9 + i for i in range(20)]
    closes = [9.5 + i for i in range(20)]
    a = atr(highs, lows, closes, 14)
    assert a is not None and a > 0


def test_quant_signal_uptrend_neutral_rsi_is_bullish():
    # Rising trend then a flat consolidation so RSI isn't pinned overbought:
    # fast SMA stays above slow (bullish) while RSI sits mid-range.
    closes = [100 + i * 0.5 for i in range(60)] + [130 + (0.4 if i % 2 else -0.4) for i in range(20)]
    bars = {"close": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes]}
    q = quant_signal(bars)
    assert q is not None
    assert q.lean > 0
    assert q.direction == "BUY"


def test_quant_signal_uptrend_overbought_cancels():
    # A pure monotonic ramp pins RSI at 100; mean-reversion fades the trend → flat.
    closes = [100 + i * 0.5 for i in range(80)]
    bars = {"close": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes]}
    q = quant_signal(bars)
    assert q is not None and q.direction == "FLAT"


def test_quant_signal_too_few_bars():
    bars = {"close": [100, 101, 102], "high": [101, 102, 103], "low": [99, 100, 101]}
    assert quant_signal(bars) is None


# ── event / macro fail-open guards ────────────────────────

def test_events_parse_day():
    from datetime import timezone
    from events import _parse_day
    d = _parse_day("2026-06-09")
    assert d is not None and d.year == 2026 and d.month == 6 and d.day == 9
    assert d.tzinfo == timezone.utc
    assert _parse_day("garbage") is None
    assert _parse_day("") is None


def test_events_blackout_disabled_fails_open(monkeypatch):
    # With no Finnhub key, blackout is disabled and never blocks (fail-open).
    import dataclasses
    import events
    from config import CONFIG
    monkeypatch.setattr(events, "CONFIG", dataclasses.replace(CONFIG, finnhub_api_key=""))
    ev = events.Events()
    try:
        assert ev.blackout("AAPL") == (False, "")
    finally:
        ev.close()


def test_macro_line_empty_without_key(monkeypatch):
    # With no FRED key, macro is disabled -> empty note, never crashes the analyst prompt.
    import dataclasses
    import macro
    from config import CONFIG
    monkeypatch.setattr(macro, "CONFIG", dataclasses.replace(CONFIG, fred_api_key=""))
    m = macro.Macro()
    try:
        assert m.line() == ""
        assert m.vix() is None
    finally:
        m.close()


# ── news sentiment ────────────────────────────────────────

def test_news_prompt_line_tags_sentiment():
    item = NewsItem(title="Chip demand surges", publisher="Reuters",
                    published_utc="2026-06-08T00:00:00Z", sentiment="positive")
    line = item.as_prompt_line()
    assert line == "[positive] Chip demand surges (Reuters)"


def test_net_sentiment_mean_signed():
    items = [
        NewsItem("a", "p", "t", "positive"),
        NewsItem("b", "p", "t", "positive"),
        NewsItem("c", "p", "t", "negative"),
        NewsItem("d", "p", "t", ""),          # unlabeled -> ignored
    ]
    # (1 + 1 - 1) / 3 labeled
    assert abs(NewsFeed.net_sentiment(items) - (1 / 3)) < 1e-9


def test_net_sentiment_empty_is_zero():
    assert NewsFeed.net_sentiment([]) == 0.0


# ── circuit breaker ───────────────────────────────────────

def test_circuit_breaker_levels():
    assert circuit_breaker(1.0) == ("green", 1.0)
    assert circuit_breaker(6.0) == ("yellow", 0.5)
    assert circuit_breaker(-11.0) == ("red", 0.0)


# ── ATR-bracket exits ─────────────────────────────────────

def _pos(side="BUY", entry=100.0, stop=95.0, target=110.0):
    return Position(symbol="TST", asset="future", side=side, qty=10, entry_price=entry,
                    size_usd=1000, stop=stop, target=target, kind="confluence",
                    thesis="t", opened_at="x", mode="paper")


def test_long_stop_and_target():
    assert should_exit(_pos(), 94.0) == "stop-loss"
    assert should_exit(_pos(), 111.0) == "take-profit"
    assert should_exit(_pos(), 100.0) is None


def test_short_stop_and_target():
    p = _pos(side="SELL", stop=105.0, target=90.0)
    assert should_exit(p, 106.0) == "stop-loss"
    assert should_exit(p, 89.0) == "take-profit"
    assert should_exit(p, 100.0) is None
