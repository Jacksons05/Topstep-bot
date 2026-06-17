"""Regime-adaptive strategy playbook for JARVIS.

Each market regime gets its own parameter set governing:
  - size_mult        : multiplier applied to the base position size (0..1)
  - min_conf         : minimum confidence score to open a new position
  - allow_long       : whether BUY signals are permitted
  - allow_short      : whether SELL signals are permitted
  - option_bias      : hint passed to the options layer ("debit"|"credit"|"spread"|"put_protect"|None)
  - atr_stop_mult    : override for ATR stop distance (None = use CONFIG default)
  - max_positions    : hard cap on concurrent open positions (None = use CONFIG default)
  - mean_reversion   : True = prefer RSI fade entries over trend-follow entries
  - notes            : human-readable description of the strategy

Regime names (from regime.py):
  "Trending"        — strong directional move, normal volatility
  "Mean-Reversion"  — oscillating / ranging (was called "Neutral" in earlier docs)
  "Consolidation"   — low vol, weak trend (tightest range)
  "Crisis"          — top-decile volatility spike / panic

Unknown / unmapped regimes default to the cautious "Unknown" entry.

Usage:
    from regime_strategy import get_regime_params
    params = get_regime_params(regime_label, CONFIG)
    if not params["allow_long"] and signal.side == "BUY":
        skip()
    size_usd = base_size * params["size_mult"]
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Playbook table
# ---------------------------------------------------------------------------

_PLAYBOOKS: dict[str, dict[str, Any]] = {
    # ── 1. TRENDING ─────────────────────────────────────────────────────────
    # Strong directional move.  Full risk-on: normal sizing, both directions,
    # momentum options (debit calls/puts or debit spreads with the trend).
    "TRENDING": {
        "size_mult": 1.0,
        "min_conf": None,           # fall back to CONFIG.confidence_threshold
        "allow_long": True,
        "allow_short": True,
        "option_bias": "debit",     # neg-gamma / momentum long single-leg
        "atr_stop_mult": None,      # use CONFIG default (2.0)
        "max_positions": None,      # use CONFIG default
        "mean_reversion": False,
        "notes": (
            "Full risk-on: momentum plays, debit options (calls/puts), "
            "normal sizing up to MAX_POSITION_PCT."
        ),
    },

    # ── 2. MEAN-REVERSION (ranging / oscillating) ────────────────────────
    # Market is chopping in a range.  Fade extremes (RSI < 30 = buy the dip,
    # RSI > 70 = sell the rip).  Prefer credit spreads / iron condors because
    # elevated IV (relative to realized) gives positive-theta premium.
    # Tighter ATR stop (1.5×) to control drawdown in chop.
    "MEAN-REVERSION": {
        "size_mult": 0.50,
        "min_conf": 0.55,           # slightly above Trending floor
        "allow_long": True,
        "allow_short": True,
        "option_bias": "credit",    # pos-gamma / credit spread / iron condor
        "atr_stop_mult": 1.5,
        "max_positions": None,
        "mean_reversion": True,
        "notes": (
            "Ranging tape: fade RSI extremes, sell premium (credit spreads / "
            "iron condors), 50 pct sizing, ATR stop at 1.5×."
        ),
    },

    # ── 3. CONSOLIDATION (low vol, tight range) ─────────────────────────
    # Price compressed — near-breakout or coiling.  Similar logic to
    # Mean-Reversion but even smaller size because breakout direction is
    # uncertain.  Defined-risk spreads only; no naked directional debit plays.
    "CONSOLIDATION": {
        "size_mult": 0.40,
        "min_conf": 0.60,
        "allow_long": True,
        "allow_short": True,
        "option_bias": "spread",    # always defined-risk (debit OR credit spread)
        "atr_stop_mult": 1.5,
        "max_positions": None,
        "mean_reversion": True,
        "notes": (
            "Compressed range / coiling: defined-risk spreads only, 40 pct "
            "sizing, conf >= 0.60. No naked debit plays; wait for breakout."
        ),
    },

    # ── 4. CRISIS (top-decile vol / panic) ──────────────────────────────
    # High realized vol — panic selling, gap risk, wide spreads.
    # Defensive: 25 pct size, max 2 concurrent positions, longs only on
    # VIX-correlated / hedging names, short equity or cash otherwise.
    # Buy puts for protection; if holding longs, sell covered calls.
    # Wider ATR stop to avoid getting chopped out by volatility spikes.
    # Require conf >= 0.80 to enter (only the highest-conviction setups).
    "CRISIS": {
        "size_mult": 0.25,
        "min_conf": 0.80,
        "allow_long": False,        # no new equity longs in crisis (short/cash only)
        "allow_short": True,
        "option_bias": "put_protect",  # buy puts / bear spreads; no unhedged calls
        "atr_stop_mult": 3.0,       # wider stop — vol spikes cause more noise
        "max_positions": 2,         # absolute cap: protect capital above all else
        "mean_reversion": False,
        "notes": (
            "Panic / high-vol: short or cash only, 25 pct sizing, max 2 positions, "
            "conf >= 0.80, buy puts / bear spreads, ATR stop widened to 3×."
        ),
    },

    # ── 5. UNKNOWN / UNCLASSIFIED (catch-all) ───────────────────────────
    # Regime detector returned something we don't recognise (edge case,
    # early-bar warmup, bad data).  Treat conservatively: 40 pct size,
    # defined-risk spreads, no entries in the final 30 min of session
    # (enforced by the engine via _past_entry_cutoff + extra_entry_cutoff).
    "UNKNOWN": {
        "size_mult": 0.40,
        "min_conf": 0.70,
        "allow_long": True,
        "allow_short": True,
        "option_bias": "spread",    # defined-risk only
        "atr_stop_mult": None,
        "max_positions": None,
        "mean_reversion": False,
        "notes": (
            "Unrecognised regime: cautious 40 pct sizing, conf >= 0.70, "
            "defined-risk spreads, no entries in final 30 min."
        ),
    },
}


def get_regime_params(regime: str, cfg: object) -> dict[str, Any]:
    """Return the playbook parameter dict for the given regime label.

    Args:
        regime: Raw regime string from classify_last() or exposure.regime.
                Case-insensitive; hyphens and spaces are normalised.
        cfg:    The CONFIG singleton (used to fill in None defaults from .env).

    Returns:
        A dict with all playbook keys resolved (no None values for numeric
        fields — they are replaced with the CONFIG defaults).
    """
    # Normalise: upper-case, strip, collapse internal whitespace
    key = regime.strip().upper().replace(" ", "-")

    # Map display variants to canonical keys
    _aliases: dict[str, str] = {
        "—": "UNKNOWN",
        "-": "UNKNOWN",
        "NEUTRAL": "MEAN-REVERSION",   # legacy name used in some docs
        "RANGING": "MEAN-REVERSION",
    }
    key = _aliases.get(key, key)

    params = dict(_PLAYBOOKS.get(key, _PLAYBOOKS["UNKNOWN"]))  # copy, never mutate

    # Resolve None → CONFIG defaults so callers always get concrete numbers
    if params["min_conf"] is None:
        params["min_conf"] = getattr(cfg, "confidence_threshold", 0.60)
    if params["atr_stop_mult"] is None:
        params["atr_stop_mult"] = getattr(cfg, "atr_stop_mult", 2.0)
    if params["max_positions"] is None:
        params["max_positions"] = getattr(cfg, "max_concurrent", 20)

    # Tag the resolved regime key for logging
    params["regime_key"] = key
    return params


def regime_allows_signal(params: dict[str, Any], side: str) -> tuple[bool, str]:
    """Check whether the regime playbook permits this signal direction.

    Returns (allowed, reason_string).
    """
    if side == "BUY" and not params["allow_long"]:
        return False, f"regime {params['regime_key']} blocks long entries (allow_long=False)"
    if side == "SELL" and not params["allow_short"]:
        return False, f"regime {params['regime_key']} blocks short entries (allow_short=False)"
    return True, ""


def apply_regime_sizing(base_size_usd: float, params: dict[str, Any]) -> float:
    """Scale the base position size by the regime's size multiplier."""
    return round(base_size_usd * params["size_mult"], 2)
