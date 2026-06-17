"""Futures contract specifications for Lucid/Rithmic integration.

Each FutureSpec contains the static data the risk layer and sizing logic need
to operate correctly on futures:

  tick_size   — minimum price movement (points)
  tick_value  — dollar value of one tick per contract (tick_size * multiplier)
  multiplier  — dollar value of one full point per contract
  margin_est  — approximate intraday margin in USD (VERIFY live with your clearing
                firm — Lucid and CME update these regularly)
  exchange    — Rithmic exchange code string
  asset_class — broad category for regime/correlation logic
  micro       — True for Micro contracts (1/10th the notional of the standard)
  parent      — root symbol of the full contract a Micro tracks (None for fulls)

Usage:
    from futures_symbols import FUTURES_SPECS, spec_for

    s = spec_for("ES")
    dollar_move = ticks * s.tick_value   # e.g. 4 ticks * $12.50 = $50
    notional    = price * s.multiplier   # e.g. 5300 * $50 = $265,000
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class FutureSpec:
    full_name: str
    exchange: str              # Rithmic exchange identifier
    tick_size: float           # minimum price increment (points/ticks)
    tick_value: float          # $ value of one tick per contract
    multiplier: float          # $ per point (tick_value / tick_size)
    margin_est: int            # estimated intraday margin (USD) — verify live!
    asset_class: str           # 'equity_index' | 'energy' | 'metal' | 'rates'
    micro: bool = False        # True for CME Micro contracts
    parent: Optional[str] = None  # full-size root (e.g. "ES" for "MES")


# ── Contract specifications ────────────────────────────────────────────────
# Margin figures are approximate CME SPAN intraday margins as of mid-2026.
# Lucid Trading typically sets tighter in-house limits — confirm before live.
#
# Tick size reference:
#   ES/MES tick  = 0.25 points  → ES tick_value = $12.50, MES = $1.25
#   NQ/MNQ tick  = 0.25 points  → NQ tick_value = $5.00,  MNQ = $0.50
#   YM/MYM tick  = 1.0 point    → YM tick_value = $5.00,  MYM = $0.50
#   CL  tick     = 0.01 $/bbl   → CL tick_value = $10.00
#   GC  tick     = 0.10 $/troy  → GC tick_value = $10.00
#   SI  tick     = 0.005 $/toz  → SI tick_value = $25.00
#   ZB  tick     = 1/32 points  → ZB tick_value = $31.25
#   ZN  tick     = 1/64 points  → ZN tick_value = $15.625

FUTURES_SPECS: dict[str, FutureSpec] = {

    # ── E-mini / Micro Equity Index (CME) ─────────────────────────────────
    "ES": FutureSpec(
        full_name="E-mini S&P 500",
        exchange="CME",
        tick_size=0.25,
        tick_value=12.50,    # $50/pt * 0.25 tick
        multiplier=50.0,
        margin_est=12_000,   # ~$12k intraday (paper: may be lower)
        asset_class="equity_index",
    ),
    "MES": FutureSpec(
        full_name="Micro E-mini S&P 500",
        exchange="CME",
        tick_size=0.25,
        tick_value=1.25,     # $5/pt * 0.25 tick  (1/10 of ES)
        multiplier=5.0,
        margin_est=1_200,
        asset_class="equity_index",
        micro=True,
        parent="ES",
    ),
    "NQ": FutureSpec(
        full_name="E-mini NASDAQ-100",
        exchange="CME",
        tick_size=0.25,
        tick_value=5.00,     # $20/pt * 0.25 tick
        multiplier=20.0,
        margin_est=17_000,
        asset_class="equity_index",
    ),
    "MNQ": FutureSpec(
        full_name="Micro E-mini NASDAQ-100",
        exchange="CME",
        tick_size=0.25,
        tick_value=0.50,     # $2/pt * 0.25 tick  (1/10 of NQ)
        multiplier=2.0,
        margin_est=1_700,
        asset_class="equity_index",
        micro=True,
        parent="NQ",
    ),
    "YM": FutureSpec(
        full_name="E-mini Dow Jones ($5)",
        exchange="CBOT",
        tick_size=1.0,
        tick_value=5.00,     # $5/pt * 1 tick
        multiplier=5.0,
        margin_est=8_000,
        asset_class="equity_index",
    ),
    "MYM": FutureSpec(
        full_name="Micro E-mini Dow Jones",
        exchange="CBOT",
        tick_size=1.0,
        tick_value=0.50,     # $0.50/pt * 1 tick  (1/10 of YM)
        multiplier=0.50,
        margin_est=800,
        asset_class="equity_index",
        micro=True,
        parent="YM",
    ),

    # ── Energy (NYMEX) ─────────────────────────────────────────────────────
    "CL": FutureSpec(
        full_name="Crude Oil (WTI)",
        exchange="NYMEX",
        tick_size=0.01,      # $0.01/barrel
        tick_value=10.00,    # 1000 bbl/contract * $0.01
        multiplier=1_000.0,
        margin_est=5_500,    # highly variable with vol; verify daily
        asset_class="energy",
    ),

    # ── Metals (COMEX) ─────────────────────────────────────────────────────
    "GC": FutureSpec(
        full_name="Gold",
        exchange="COMEX",
        tick_size=0.10,      # $0.10/troy oz
        tick_value=10.00,    # 100 oz/contract * $0.10
        multiplier=100.0,
        margin_est=8_500,
        asset_class="metal",
    ),
    "SI": FutureSpec(
        full_name="Silver",
        exchange="COMEX",
        tick_size=0.005,     # $0.005/troy oz
        tick_value=25.00,    # 5000 oz/contract * $0.005
        multiplier=5_000.0,
        margin_est=9_000,    # silver margin is notoriously spiky
        asset_class="metal",
    ),

    # ── Interest Rates (CBOT) ──────────────────────────────────────────────
    "ZB": FutureSpec(
        full_name="30-Year US Treasury Bond",
        exchange="CBOT",
        tick_size=1 / 32,    # 1/32nd of a point (= 0.03125)
        tick_value=31.25,    # $1000/pt * (1/32)
        multiplier=1_000.0,
        margin_est=3_000,
        asset_class="rates",
    ),
    "ZN": FutureSpec(
        full_name="10-Year US Treasury Note",
        exchange="CBOT",
        tick_size=1 / 64,    # 1/64th of a point (= 0.015625)
        tick_value=15.625,   # $1000/pt * (1/64)
        multiplier=1_000.0,
        margin_est=1_800,
        asset_class="rates",
    ),
}


def spec_for(symbol: str) -> FutureSpec | None:
    """Return the FutureSpec for a root symbol (case-insensitive). None if unknown."""
    return FUTURES_SPECS.get(symbol.upper())


def is_futures_symbol(symbol: str) -> bool:
    """True when symbol is a known futures root in our spec table."""
    return symbol.upper() in FUTURES_SPECS


def dollar_value_per_point(symbol: str) -> float:
    """Multiplier ($/point) for a symbol, or 1.0 as a safe fallback for unknowns."""
    spec = spec_for(symbol)
    return spec.multiplier if spec else 1.0


def tick_value_for(symbol: str) -> float:
    """Dollar value of one minimum tick move, or 0.0 when symbol is unknown."""
    spec = spec_for(symbol)
    return spec.tick_value if spec else 0.0


def intraday_margin(symbol: str) -> int:
    """Estimated intraday margin in USD. 0 when symbol is unknown.

    WARNING: These are rough estimates. Always verify with your clearing firm
    before sizing positions — margins change with volatility and CME policy.
    """
    spec = spec_for(symbol)
    return spec.margin_est if spec else 0
