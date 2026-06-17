"""Deterministic unit tests — no network, no API key.

Covers the indicator math, the options-exposure level derivation, the risk
gate's safety layers, and ATR-bracket exits.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import NewsFeed, NewsItem  # noqa: E402
from options import ExposureProfile, StrikeExposure, compute_levels, _fa_strike_rows  # noqa: E402
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


# ── options exposure levels ───────────────────────────────

def test_gamma_flip_and_walls():
    spot = 100.0
    strikes = [
        StrikeExposure(90, gex=-300),
        StrikeExposure(95, gex=-100),
        StrikeExposure(100, gex=50),
        StrikeExposure(105, gex=500),   # call wall
        StrikeExposure(110, gex=200),
    ]
    p = compute_levels(ExposureProfile("TST", spot, strikes))
    assert p.net_gex == 350
    assert p.regime == "positive-gamma"
    assert p.call_wall == 105          # max gex at/above spot
    # cumulative GEX crosses zero between strikes 100 (cum -350) and 105 (cum +150)
    assert p.gamma_flip is not None and 100 <= p.gamma_flip <= 105


def test_fa_strike_rows_columnar():
    # FlashAlpha columnar shape: parallel arrays. put_gex pre-signed negative.
    rows = _fa_strike_rows({
        "strikes": [95, 100, 105],
        "call_gex": [10, 80, 500],
        "put_gex": [-300, -60, -20],
    })
    assert [r.strike for r in rows] == [95.0, 100.0, 105.0]
    assert rows[0].gex == -290   # 10 + (-300)
    assert rows[2].gex == 480    # 500 + (-20)


def test_fa_strike_rows_rowwise_and_feeds_levels():
    # FlashAlpha row-wise shape: array of per-strike objects -> compute_levels works on it.
    rows = _fa_strike_rows({"strikes": [
        {"strike": 90, "call_gex": 5, "put_gex": -305},
        {"strike": 100, "call_gex": 70, "put_gex": -20},
        {"strike": 105, "call_gex": 500, "put_gex": -10},
    ]})
    p = compute_levels(ExposureProfile("TST", 100.0, rows))
    assert p.call_wall == 105
    assert p.regime in ("positive-gamma", "negative-gamma")


def test_flashalpha_skips_nonallowlisted_symbol():
    # Symbol outside FLASHALPHA_SYMBOLS must never hit the network -> None, no spend.
    import options as opt
    opt._FA_CACHE.clear()
    opt._FA_CALLS["date"], opt._FA_CALLS["n"] = "", 0
    assert opt._cached_flashalpha("ZZZZ_NOT_ALLOWED", 100.0, None) is None
    assert int(opt._FA_CALLS["n"]) == 0


def test_flashalpha_budget_blocks_when_exhausted():
    # With the daily budget spent, an allowlisted symbol returns cache (None) w/o fetching.
    import options as opt
    from datetime import date
    opt._FA_CACHE.clear()
    sym = next(iter(opt.FLASHALPHA_SYMBOLS))
    opt._FA_CALLS["date"], opt._FA_CALLS["n"] = date.today().isoformat(), opt.FLASHALPHA_DAILY_BUDGET
    # budget remaining 0 (<2) -> no network, returns None (no cache present)
    assert opt._cached_flashalpha(sym, 100.0, None) is None


def test_flashalpha_serves_fresh_cache():
    # A fresh cache entry is returned without spending budget.
    import time as _t
    import options as opt
    opt._FA_CALLS["date"], opt._FA_CALLS["n"] = "", 0
    sym = next(iter(opt.FLASHALPHA_SYMBOLS))
    sentinel = ExposureProfile(sym, 100.0, [])
    opt._FA_CACHE[sym] = (_t.time(), sentinel)
    assert opt._cached_flashalpha(sym, 100.0, None) is sentinel
    assert int(opt._FA_CALLS["n"]) == 0


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


def _chain_with(legs_quotes):
    # build a synthetic CboeChain from {occ: (bid, ask, oi)} for pricing tests
    from datetime import date
    from options import CboeChain, ContractQuote
    by_occ = {}
    for occ, (bid, ask, oi) in legs_quotes.items():
        from options import _parse_occ
        _, expiry, is_call, strike = _parse_occ(occ)
        by_occ[occ] = ContractQuote(occ=occ, strike=strike, is_call=is_call, expiry=expiry,
                                     bid=bid, ask=ask, oi=oi, volume=10, delta=0.5, gamma=0.01,
                                     theta=-0.1, vega=0.1, iv=0.2)
    return CboeChain(symbol="SPY", spot=600.0, front_expiry=date(2026, 6, 27),
                     strike_step=1.0, by_occ=by_occ, strikes=sorted({q.strike for q in by_occ.values()}))


def test_contract_quote_liquidity():
    from datetime import date
    from options import ContractQuote
    q = ContractQuote("X", 600, True, date(2026, 6, 27), bid=1.00, ask=1.10, oi=500,
                      volume=10, delta=0.5, gamma=0.01, theta=-0.1, vega=0.1, iv=0.2)
    assert q.mid == 1.05
    assert q.liquid(max_spread_pct=0.20, min_mid=0.05)
    wide = ContractQuote("Y", 600, True, date(2026, 6, 27), bid=0.10, ask=1.00, oi=500,
                         volume=1, delta=0.5, gamma=0.01, theta=-0.1, vega=0.1, iv=0.2)
    assert not wide.liquid(0.20, 0.05)   # spread way over 20%


def test_price_structure_debit_long():
    from datetime import date
    from options_strategy import price_structure, select_structure
    p = _exposure(-1_000_000)  # negative gamma -> long_call, ATM strike 600
    st = select_structure(p, "BUY", date(2026, 6, 27), strike_step=1.0)
    chain = _chain_with({st.legs[0].occ: (2.00, 2.10, 500)})
    assert price_structure(chain, st, min_mid=0.05)
    assert st.entry_value == 2.05 and st.max_loss == 205.0  # premium*100


def test_price_structure_credit_spread_and_illiquid():
    from datetime import date
    from options_strategy import price_structure, select_structure
    p = _exposure(+1_000_000)  # positive gamma -> bull_put 595/594
    st = select_structure(p, "BUY", date(2026, 6, 27), strike_step=1.0, spread_width=1.0)
    short = [l for l in st.legs if l.side == "sell"][0]
    long = [l for l in st.legs if l.side == "buy"][0]
    chain = _chain_with({short.occ: (0.60, 0.66, 800), long.occ: (0.12, 0.16, 800)})
    assert price_structure(chain, st, spread_width=1.0, min_mid=0.05)
    assert st.entry_value < 0                 # net credit collected
    assert 0 < st.max_loss <= 100             # (width - credit)*100, width=1
    # drop a leg's liquidity -> rejected
    bad = _chain_with({short.occ: (0.60, 2.00, 800), long.occ: (0.12, 0.16, 800)})
    assert not price_structure(bad, st, spread_width=1.0, min_mid=0.05)


def test_directional_levels_correct_side():
    from options import ExposureProfile
    from options_strategy import directional_levels
    spot = 600.0
    p = ExposureProfile("SPY", spot, [])
    p.call_wall, p.put_wall, p.zero_dte_magnet, p.gamma_flip = 605.0, 595.0, 601.0, 596.0
    # bullish: target above spot, stop below
    stop, target = directional_levels("BUY", spot, p, atr=2.0)
    assert stop < spot < target
    assert target == 601.0 and stop == 596.0   # magnet above, flip below -> used directly
    # bearish: target below, stop above
    stop, target = directional_levels("SELL", spot, p, atr=2.0)
    assert target < spot < stop


def test_directional_levels_falls_back_when_wrong_side():
    from options import ExposureProfile
    from options_strategy import directional_levels
    spot = 600.0
    p = ExposureProfile("SPY", spot, [])
    # magnet BELOW spot but trade is bullish -> must NOT use magnet as target
    p.call_wall, p.put_wall, p.zero_dte_magnet, p.gamma_flip = 607.0, 590.0, 598.0, 595.0
    stop, target = directional_levels("BUY", spot, p, atr=2.0)
    assert target > spot          # fell back to call_wall (607), not the below-spot magnet
    assert target == 607.0
    assert stop < spot


def test_parse_occ_roundtrip():
    from datetime import date
    from options import _parse_occ
    from options_strategy import occ_symbol
    occ = occ_symbol("SPY", date(2026, 6, 27), False, 612.5)  # SPY260627P00612500
    root, expiry, is_call, strike = _parse_occ(occ)
    assert root == "SPY" and expiry == date(2026, 6, 27)
    assert is_call is False and strike == 612.5
    assert _parse_occ("garbage") is None


def test_occ_symbol_format():
    from datetime import date
    from options_strategy import occ_symbol
    assert occ_symbol("SPY", date(2026, 6, 27), False, 400) == "SPY260627P00400000"
    assert occ_symbol("spy", date(2026, 6, 27), True, 612.5) == "SPY260627C00612500"


def _exposure(regime_net_gex, spot=600.0):
    # build a minimal profile with explicit levels for structure tests
    p = ExposureProfile("SPY", spot, [])
    p.net_gex = regime_net_gex
    p.call_wall = 605.0
    p.put_wall = 595.0
    p.zero_dte_magnet = 601.0
    return p


def test_negative_gamma_goes_long_single_leg():
    from datetime import date
    from options_strategy import select_structure
    p = _exposure(-1_000_000)            # negative gamma
    s = select_structure(p, "BUY", date(2026, 6, 27))
    assert s.kind == "long_call" and s.is_debit and len(s.legs) == 1
    assert s.legs[0].side == "buy" and s.legs[0].strike == 600.0  # ATM
    assert s.target == 601.0             # 0DTE magnet
    s2 = select_structure(p, "SELL", date(2026, 6, 27))
    assert s2.kind == "long_put" and s2.legs[0].is_call is False


def test_positive_gamma_builds_credit_spread_at_walls():
    from datetime import date
    from options_strategy import select_structure
    p = _exposure(+1_000_000)            # positive gamma
    bull = select_structure(p, "BUY", date(2026, 6, 27))
    assert bull.kind == "bull_put" and not bull.is_debit and bull.is_multi_leg
    # short put at put wall (595), long put one width lower (594)
    short = [l for l in bull.legs if l.side == "sell"][0]
    long = [l for l in bull.legs if l.side == "buy"][0]
    assert short.strike == 595.0 and long.strike == 594.0
    bear = select_structure(p, "SELL", date(2026, 6, 27))
    assert bear.kind == "bear_call"
    short_c = [l for l in bear.legs if l.side == "sell"][0]
    assert short_c.strike == 605.0 and short_c.is_call  # short call at call wall


def test_flat_direction_returns_none():
    from datetime import date
    from options_strategy import select_structure
    assert select_structure(_exposure(-1), "HOLD", date(2026, 6, 27)) is None


def test_simbroker_submit_option_opening_and_closing():
    from datetime import date
    from broker import SimBroker
    from options_strategy import select_structure
    p = _exposure(-1_000_000)
    st = select_structure(p, "BUY", date(2026, 6, 27))   # long_call, single leg
    b = SimBroker()
    f_open = b.submit_option(st, qty=3, ref_price=600.0, opening=True)
    assert f_open.side == "BUY" and f_open.qty == 3 and f_open.status == "filled"
    assert f_open.symbol == st.legs[0].occ
    f_close = b.submit_option(st, qty=3, ref_price=601.0, opening=False)
    assert f_close.side == "SELL"


def test_option_legs_json_roundtrip():
    # Mirrors executor: serialize structure legs -> JSON -> rebuild for the close order.
    import json
    from datetime import date
    from options_strategy import OptionLeg, OptionStructure, select_structure
    st = select_structure(_exposure(+1_000_000), "SELL", date(2026, 6, 27))  # bear_call spread
    legs_json = json.dumps([
        {"occ": l.occ, "side": l.side, "is_call": l.is_call, "strike": l.strike, "ratio": l.ratio}
        for l in st.legs
    ])
    rebuilt = OptionStructure(kind=st.kind, legs=[
        OptionLeg(occ=d["occ"], side=d["side"], is_call=d["is_call"],
                  strike=d["strike"], ratio=d.get("ratio", 1))
        for d in json.loads(legs_json)
    ])
    assert [l.occ for l in rebuilt.legs] == [l.occ for l in st.legs]
    assert rebuilt.is_multi_leg


def test_near_wall_detection():
    p = ExposureProfile("TST", 100.0, [])
    p.call_wall = 100.3
    assert p.near_wall(0.5) == "call_wall"
    assert p.near_wall(0.1) is None


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
    return Position(symbol="TST", asset="equity", side=side, qty=10, entry_price=entry,
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
