"""Deterministic tests for the order-flow engine — no network, synthetic feeds."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orderflow import OrderFlowEngine, MultiLevelBook, mad_modified_z  # noqa: E402


def test_multilevel_book_depth_obi():
    b = MultiLevelBook()
    b.apply_snapshot(
        bids=[(99.0, 300), (98.0, 400), (97.0, 300)],   # ΣLb (top 3) = 1000
        asks=[(100.0, 50), (101.0, 50), (102.0, 100)],  # ΣLa (top 3) = 200
    )
    assert b.has_depth
    assert abs(b.depth_obi(3) - 0.666666) < 1e-4         # (1000-200)/1200
    assert b.best_bid() == (99.0, 300) and b.best_ask() == (100.0, 50)


def test_multilevel_book_update_add_and_remove():
    b = MultiLevelBook()
    b.apply_snapshot([(99.0, 100)], [(100.0, 100)])
    b.apply_update("bid", 98.0, 500)         # add a level
    assert b.depth_obi(5) > 0                 # bids now heavier
    b.apply_update("bid", 98.0, 0)           # remove it
    assert 98.0 not in b.bids


def test_engine_prefers_depth_obi_over_bbo():
    e = OrderFlowEngine()
    # top-of-book says balanced...
    e.on_depth(bid=100.0, bid_size=100, ask=100.25, ask_size=100)
    assert e.obi == 0.0
    # ...but the L2 ladder is bid-heavy → OBI flips positive and gate confirms BUY
    e.on_depth_snapshot(
        bids=[(100.0, 800), (99.75, 600)],
        asks=[(100.25, 50), (100.5, 50)],
    )
    assert e.has_data and e.obi > 0.85
    ok, reason = e.confirm_entry("BUY")
    assert ok and "OBI" in reason
from rithmic_marketdata import RithmicOrderFlowFeed  # noqa: E402


class _MockBroker:
    _mock_mode = True
    _client = None
    def _run(self, coro):  # never called in mock
        raise AssertionError("should not run in mock mode")


def test_feed_mock_mode_subscribes_nothing_and_gate_fails_open():
    feed = RithmicOrderFlowFeed(_MockBroker())
    assert feed.subscribe(["ES", "MNQ", "SPY"]) == 0   # mock → no live subscription
    eng = feed.get("ES")
    assert not eng.has_data                              # cold → gate fails open
    assert eng.multiplier == 50.0                        # ES $/point multiplier wired


def test_obi_bid_heavy_is_positive():
    e = OrderFlowEngine()
    e.on_depth(bid=100.0, bid_size=900, ask=100.25, ask_size=100)
    assert abs(e.obi - 0.8) < 1e-9          # (900-100)/1000
    e.on_depth(bid=100.0, bid_size=100, ask=100.25, ask_size=900)
    assert abs(e.obi + 0.8) < 1e-9          # ask-heavy → negative


def test_micro_price_leans_toward_thin_side():
    e = OrderFlowEngine()
    # bid liquidity huge, ask thin → micro-price weighted toward the ask (price about to lift)
    e.on_depth(bid=100.0, bid_size=1000, ask=101.0, ask_size=10)
    mp = e.micro_price
    assert 100.0 < mp <= 101.0 and mp > 100.5


def test_cvd_aggressor_classification():
    e = OrderFlowEngine()
    e.on_depth(bid=100.0, bid_size=50, ask=100.25, ask_size=50)
    assert e.on_trade(100.25, 10) == 1      # at ask → buy
    assert e.on_trade(100.00, 4) == -1      # at bid → sell
    assert e.cvd == 6                        # +10 - 4


def test_mad_z_flags_outlier():
    base = [100.0] * 20
    assert mad_modified_z(100.0, base) == 0.0      # MAD 0 → no flag
    series = [100, 110, 90, 105, 95, 100, 2000]    # last is a whale
    assert mad_modified_z(2000, series) > 3.5


def test_whale_flag_requires_z_and_notional():
    e = OrderFlowEngine(multiplier=1.0)
    e.on_depth(bid=100.0, bid_size=50, ask=100.25, ask_size=50)
    # seed small VARIED buckets across distinct seconds (varied → MAD > 0)
    for s, sz in enumerate([10, 12, 8, 11, 9], start=1):
        e.on_trade(100.25, sz, ts=float(s))        # ~$1k each, different sizes
    # a $2M buy print in a fresh second → z>3.5 and notional≥$1M
    e.on_trade(100.25, 20000, ts=10.0)             # 100.25*20000 ≈ $2.0M
    assert e.whale() == 1


def test_confirm_entry_needs_extreme_obi():
    e = OrderFlowEngine()
    e.on_depth(bid=100.0, bid_size=500, ask=100.25, ask_size=500)  # OBI 0 → no
    ok, _ = e.confirm_entry("BUY")
    assert not ok
    e.on_depth(bid=100.0, bid_size=960, ask=100.25, ask_size=40)   # OBI 0.92 ≥ 0.85
    ok, reason = e.confirm_entry("BUY")
    assert ok and "OBI" in reason


def test_confirm_entry_vetoes_on_opposing_cvd_divergence():
    e = OrderFlowEngine()
    e.on_depth(bid=100.0, bid_size=960, ask=100.25, ask_size=40)   # strong buy OBI
    # build a bearish divergence: price grinds to new highs while CVD rolls over
    for i in range(15):
        # sells at the bid push CVD down even as price ticks up
        e.on_depth(bid=100.0 + i * 0.1, bid_size=960, ask=100.25 + i * 0.1, ask_size=40)
        e.on_trade(100.0 + i * 0.1, 5, ts=float(i))   # at/below bid → negative delta
    ok, reason = e.confirm_entry("BUY")
    assert not ok and "divergence" in reason
