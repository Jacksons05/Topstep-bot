"""Tests for the reusable research scaffolding (research/*).

The important one is test_reproduces_round26: the new framework must
reproduce the already-published Round 26 result (PF ~1.26 at 1 tick,
collapsing at 2 ticks). A framework that can't reproduce a known round is
not trustworthy for the next one.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from research import backtest as bt
from research import datasets as ds
from research import features as ft


# ── fill-rule invariants (the Round-24 post-mortem, encoded) ────────────────

class _Bars:
    """Minimal bars stub: one session of 5-min bars."""
    def __init__(self, o, h, l, c):
        self.sym = "ES"
        self.o, self.h, self.l, self.c = (np.array(x, float) for x in (o, h, l, c))
        self.v = np.ones(len(o))
        self.ts = [None] * len(o)

    def minute_of_day(self, i):
        return 9 * 60 + 30 + 5 * i


def test_entry_is_next_bar_open():
    b = _Bars(o=[100, 101, 101], h=[100.5, 101.5, 101.5],
              l=[99.5, 100.5, 100.5], c=[100, 101, 101])
    t = bt.simulate_bracket(b, [0, 1, 2], 0, side=1, stop=99.0, target=103.0)
    assert t is not None and t.entry_px == 101.0    # bar 1's OPEN, not bar 0's close


def test_never_exits_on_entry_bar():
    # entry bar (index 1) has a huge range that would hit both stop and target
    b = _Bars(o=[100, 101, 101], h=[100.5, 110.0, 101.2],
              l=[99.5, 90.0, 100.8], c=[100, 101, 101])
    t = bt.simulate_bracket(b, [0, 1, 2], 0, side=1, stop=99.0, target=103.0)
    assert t is not None and t.exit_i != 1          # exit cannot be the entry bar


def test_invalid_geometry_is_skipped():
    b = _Bars(o=[100, 105, 105], h=[100.5, 105.5, 105.5],
              l=[99.5, 104.5, 104.5], c=[100, 105, 105])
    # long with target BELOW the next-open entry → degenerate, must skip
    assert bt.simulate_bracket(b, [0, 1, 2], 0, side=1, stop=99.0, target=103.0) is None


def test_stop_wins_when_both_hit_in_one_bar():
    b = _Bars(o=[100, 101, 101], h=[100.5, 101.5, 110.0],
              l=[99.5, 100.5, 90.0], c=[100, 101, 101])
    t = bt.simulate_bracket(b, [0, 1, 2], 0, side=1, stop=99.0, target=103.0)
    assert t is not None and t.reason == "stop"


def test_costs_applied_both_sides():
    b = _Bars(o=[100, 100, 100], h=[100, 100, 100], l=[100, 100, 100], c=[100, 100, 100])
    t = bt.Trade(0, 2, 100.0, 100.0, 1, "flatten")     # flat move
    pnl = bt.net_pnl([t], "ES", slippage_ticks=1.0)[0]
    # ES: $4 comm + 2 * 1 tick * 0.25 * $50 = $29 total cost
    assert pnl == pytest.approx(-29.0)


# ── metrics ─────────────────────────────────────────────────────────────────

def test_metric_kernel_shapes():
    b = _Bars(o=[100] * 5, h=[100] * 5, l=[100] * 5, c=[100] * 5)
    b.ts = [type("T", (), {"year": 2024})() for _ in range(5)]
    trades = [bt.Trade(0, 1, 100.0, 102.0, 1, "target"),
              bt.Trade(1, 2, 100.0, 99.0, 1, "stop"),
              bt.Trade(2, 3, 100.0, 103.0, 1, "target")]
    m = bt.evaluate(trades, b, "ES", monte_carlo=False)
    for k in ("win_pct", "pf", "sharpe_per_trade", "sortino_per_trade",
              "max_drawdown_usd", "max_consec_wins", "max_consec_losses",
              "expectancy_usd", "p_bootstrap", "yearly_usd"):
        assert k in m, f"missing metric {k}"


def test_monte_carlo_ruin_bounds():
    pnl = np.array([100.0, -50.0, 75.0, -120.0, 200.0] * 20)
    mc = bt.monte_carlo_ruin(pnl, n_paths=500)
    assert 0.0 <= mc["p_ruin"] <= 1.0
    assert 0.0 <= mc["p_target_first"] <= 1.0


def test_pass_bar_rejects_marginal_result():
    weak = {"n": 1000, "pf": 1.09, "p_one_sided": 0.18,
            "p_bootstrap": 0.18, "pct_years_positive": 41.2}   # Round 26 @ 2 ticks
    assert bt.passes(weak) is False
    strong = {"n": 1000, "pf": 1.30, "p_one_sided": 0.001,
              "p_bootstrap": 0.001, "pct_years_positive": 75.0}
    assert bt.passes(strong) is True


# ── feature causality ───────────────────────────────────────────────────────

def test_value_area_encloses_target_pct():
    closes = [100, 100, 101, 101, 101, 102, 103]
    vols = [10, 10, 50, 50, 50, 10, 5]
    poc, vah, val = ft.value_area(closes, vols)
    assert poc == 101.0 and val <= poc <= vah


def test_gex_regimes_are_shifted_forward():
    """A session's GEX regime must come from a STRICTLY EARLIER close."""
    reg = ft.gex_regime_for_session()
    if not reg:
        pytest.skip("no GEX series on disk")
    raw = ds.load_gex_daily()
    d = max(raw)
    assert d not in reg or reg[d]["gex"] != raw[d], "same-day GEX leaked into its own session"


# ── the real check: reproduce a published round ─────────────────────────────

@pytest.mark.slow
def test_reproduces_round26():
    """Framework must reproduce Round 26 (overnight inventory reversal):
    PF ~1.26 / t ~2.3 at 1 tick, and the 2-tick collapse."""
    bars = ds.load_bars("ES")
    a = ft.atr(bars.h, bars.l, bars.c)
    on = ft.overnight_features(bars)
    sessions = bars.rth_sessions()

    trades = []
    for day in sorted(sessions):
        info = on.get(day)
        if not info or not info["on_top_tercile"]:
            continue
        idxs = sessions[day]
        atr0 = a[idxs[0]]
        if np.isnan(atr0) or atr0 <= 0:
            continue
        side = -1 if info["on_move"] > 0 else 1
        entry_ref = info["rth_open"]
        target = info["prev_rth_close"]
        stop = entry_ref + (atr0 if side < 0 else -atr0)
        # signal_k = -1 so the simulator enters at idxs[0]'s OPEN (the 09:30 open)
        t = bt.simulate_bracket(bars, idxs, -1, side, stop, target,
                                flatten_minute=15 * 60 + 55)
        if t is not None:
            trades.append(t)

    one = bt.evaluate(trades, bars, "ES", slippage_ticks=1.0, monte_carlo=False)
    two = bt.evaluate(trades, bars, "ES", slippage_ticks=2.0, monte_carlo=False)
    assert one["n"] > 1000
    assert one["pf"] == pytest.approx(1.26, abs=0.06), one
    assert one["t"] == pytest.approx(2.3, abs=0.4), one
    # and the documented fragility: 2-tick slippage kills significance
    assert two["t"] < 1.5 and not bt.passes(two)
