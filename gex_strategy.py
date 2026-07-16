"""GEX-regime entry signals (Phase 4) — replaces SMA20/50+RSI when
ENTRY_ENGINE=gex.

Produces the same QuantRead contract signals.quant_signal emits, so every
downstream gate (LLM confluence, Topstep risk walls, order-flow confirmation,
regime playbook, sizing) applies unchanged. Only the *entry idea* differs:

    positive gamma  → VWAP mean-reversion: dealers hedge against moves, so
                      stretches ≥ GEX_MR_ATR_DEV × ATR away from the session
                      VWAP tend to revert toward it. Fade the stretch.
    negative gamma  → breakout momentum: dealer hedging amplifies moves, so a
                      close through the N-bar high/low continues. Follow it.
                      Engine applies GEX_NEG_RISK_MULT (< 1) so these run at
                      reduced (micro-tier) risk.
    neutral         → None. No edge claimed; entries stay locked.

The VWAP directional gate in engine._prescreen (longs above / shorts below
VWAP, skip-if-extended) is BYPASSED in gex mode: the mean-reversion leg
deliberately buys below / sells above VWAP, which is the exact opposite of
that gate's momentum logic.

Lean strength scales with the size of the stretch/break (capped at 1.0), so
the existing confidence thresholds keep discriminating rather than every
signal arriving at a fixed conviction.
"""
from __future__ import annotations

import math

from config import CONFIG
from signals import QuantRead, atr as _atr


def _bars_ok(bars: dict, need: int) -> bool:
    closes = bars.get("close") or []
    highs = bars.get("high") or []
    lows = bars.get("low") or []
    return len(closes) >= need and len(highs) >= need and len(lows) >= need


def gex_quant_signal(bars: dict, regime: str, vwap: float | None) -> QuantRead | None:
    """Entry read for the current GEX regime. None = no entry (including the
    whole neutral regime, and any degenerate data — fail closed)."""
    if regime == "neutral":
        return None
    need = max(CONFIG.gex_breakout_lookback + 1, 15)
    if not _bars_ok(bars, need):
        return None
    closes, highs, lows = bars["close"], bars["high"], bars["low"]
    px = closes[-1]
    a = _atr(highs, lows, closes, 14)
    if not (px and px > 0 and a and math.isfinite(a) and a > 0):
        return None

    if regime == "positive":
        # ── VWAP mean-reversion (vol suppressed) ─────────────────────────
        if vwap is None or not (math.isfinite(vwap) and vwap > 0):
            return None
        dev = (px - vwap) / a  # stretch from fair value, in ATRs
        k = CONFIG.gex_mr_atr_dev
        if dev <= -k:
            lean = min(1.0, abs(dev) / (2.0 * k))   # k ATRs → 0.5, 2k → 1.0
            return QuantRead(lean=round(lean, 4), strength=round(lean, 4), atr=a,
                             detail=f"GEX+ VWAP-MR: {abs(dev):.2f} ATR below vwap "
                                    f"{vwap:.2f} → revert long")
        if dev >= k:
            lean = -min(1.0, abs(dev) / (2.0 * k))
            return QuantRead(lean=round(lean, 4), strength=round(abs(lean), 4), atr=a,
                             detail=f"GEX+ VWAP-MR: {abs(dev):.2f} ATR above vwap "
                                    f"{vwap:.2f} → revert short")
        return None

    if regime == "negative":
        # ── Breakout momentum (vol expanded, dealer hedging chases) ──────
        n = CONFIG.gex_breakout_lookback
        prior_high = max(highs[-(n + 1):-1])
        prior_low = min(lows[-(n + 1):-1])
        if px > prior_high:
            brk = (px - prior_high) / a
            lean = min(1.0, 0.5 + brk)              # any break ≥ 0.5, +1 ATR → 1.0
            return QuantRead(lean=round(lean, 4), strength=round(lean, 4), atr=a,
                             detail=f"GEX- breakout: close {px:.2f} > {n}-bar high "
                                    f"{prior_high:.2f} (+{brk:.2f} ATR) → momentum long")
        if px < prior_low:
            brk = (prior_low - px) / a
            lean = -min(1.0, 0.5 + brk)
            return QuantRead(lean=round(lean, 4), strength=round(abs(lean), 4), atr=a,
                             detail=f"GEX- breakout: close {px:.2f} < {n}-bar low "
                                    f"{prior_low:.2f} (-{brk:.2f} ATR) → momentum short")
        return None

    return None  # unknown regime label — fail closed
