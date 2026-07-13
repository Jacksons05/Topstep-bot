"""Flow-risk overlays: volatility-target sizing (A10) + toxicity veto (A8).

Two research-validated risk overlays that layer on top of risk.py / lucid_risk.py
WITHOUT replacing them. Both are *self-calibrating* against each symbol's own
recent history, so there are no magic annualization constants and they work on
whatever timeframe the engine feeds them (daily or intraday bars).

  A10  Vol-target sizing
       Scale position size inversely to a HAR-style realized-vol forecast,
       relative to the symbol's own normal vol. Elevated vol -> size DOWN;
       calm vs its own norm -> size UP (capped). This is the piece that most
       directly serves the Topstep drawdown constraint: it stabilizes per-trade
       risk instead of betting constant notional into changing volatility.

  A8   Toxicity veto (VPIN-style)
       Compute a bulk-volume-classified (BVC) VPIN over the bars and STAND ASIDE
       when current flow toxicity sits in the top tail of the symbol's own recent
       distribution. Rationale + honest caveat: the pre-check and the replication
       literature (Andersen & Bondarenko 2014) show VPIN's content is largely a
       mechanical function of volume/volatility -- so we use it ONLY as a
       stand-aside RISK filter, never as a directional alpha signal.

Design: pure-numpy, no scipy (matches the rest of the live engine, which must not
pull heavy deps). All strings ASCII-only for the cp1252 Windows console.

Usage in engine.py:
    from flow_risk import FlowRiskManager
    self._flow = FlowRiskManager() if CONFIG.vol_sizing_enabled or \
                 CONFIG.toxicity_veto_enabled else None
    # in _prescreen, where `bars` is already in hand (no extra fetch):
    if self._flow is not None:
        self._flow_reads[sym] = self._flow.assess(bars)
    # in the execute loop:
    fr = self._flow_reads.get(sig.symbol)
    if fr and fr.veto:            # A8
        continue
    size *= fr.vol_mult          # A10  (fr.vol_mult == 1.0 when disabled/missing)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from config import CONFIG


@dataclass
class FlowRead:
    """Per-symbol flow-risk snapshot produced from one bars() payload."""
    vol_mult: float = 1.0        # A10 size multiplier, already clamped
    vol_basis: str = "flat"      # human-readable basis for logging
    toxicity: float = 0.0        # latest BVC-VPIN in [0, 1]
    tox_pct: float = 0.0         # its percentile within the symbol's own history
    veto: bool = False           # A8: True => stand aside
    veto_reason: str = ""


def _log_returns(closes: np.ndarray) -> np.ndarray:
    closes = np.asarray(closes, dtype=float)
    closes = closes[closes > 0]
    if closes.size < 2:
        return np.array([])
    return np.diff(np.log(closes))


def _rv(logret: np.ndarray, window: int) -> float:
    """Realized vol (std of log returns) over the last `window` observations."""
    if logret.size == 0:
        return 0.0
    w = logret[-window:] if logret.size >= window else logret
    if w.size < 2:
        return 0.0
    return float(np.std(w, ddof=1))


def har_vol_forecast(closes) -> float:
    """HAR-style vol forecast: the component average of short/weekly/monthly
    realized vol. Captures long-memory (recent + weekly + monthly vol all
    matter) without fitting weights online. Units are per-bar std of log return."""
    lr = _log_returns(closes)
    if lr.size < 3:
        return 0.0
    comps = [_rv(lr, 5), _rv(lr, 22), _rv(lr, 66)]
    comps = [c for c in comps if c > 0]
    return float(np.mean(comps)) if comps else 0.0


def _baseline_vol(closes) -> float:
    """The symbol's 'normal' short-term vol: median of the rolling RV(5) series.
    Robust to spikes, so the vol-target ratio measures deviation from normalcy."""
    lr = _log_returns(closes)
    if lr.size < 6:
        return _rv(lr, 5)
    roll = np.array([
        np.std(lr[max(0, i - 5 + 1): i + 1], ddof=1)
        for i in range(4, lr.size)
        if (i + 1) - max(0, i - 5 + 1) >= 2
    ])
    roll = roll[np.isfinite(roll) & (roll > 0)]
    return float(np.median(roll)) if roll.size else har_vol_forecast(closes)


def vol_target_multiplier(closes) -> tuple[float, str]:
    """A10: multiplier = (baseline_vol / forecast_vol) * VOL_TARGET_RATIO, clamped.

    > 1 when current forecast vol is below the symbol's own norm (calm) up to
    VOL_SIZING_CAP; < 1 when vol is elevated, down to VOL_SIZING_FLOOR."""
    forecast = har_vol_forecast(closes)
    baseline = _baseline_vol(closes)
    if forecast <= 0 or baseline <= 0:
        return 1.0, "vol=flat(mult 1.00)"
    raw = (baseline / forecast) * CONFIG.vol_target_ratio
    mult = float(np.clip(raw, CONFIG.vol_sizing_floor, CONFIG.vol_sizing_cap))
    basis = (f"volfc={forecast:.4f} base={baseline:.4f} "
             f"raw={raw:.2f} mult={mult:.2f}")
    return mult, basis


def _phi(x: np.ndarray) -> np.ndarray:
    """Standard normal CDF, vectorized (no scipy)."""
    return 0.5 * (1.0 + np.vectorize(math.erf)(x / math.sqrt(2.0)))


def bvc_vpin_series(closes, volumes, window: int) -> np.ndarray:
    """Rolling bulk-volume-classified VPIN over bars.

    For each bar, classify its volume into buy/sell fractions from the
    standardized close-to-close change (Easley-Lopez de Prado-O'Hara BVC), then
    VPIN over a rolling `window` = sum(vol * |2*buyfrac - 1|) / sum(vol). Returns
    the series of rolling VPIN values in [0, 1]. Falls back to equal bar weights
    when volume is absent/zero."""
    closes = np.asarray(closes, dtype=float)
    if closes.size < window + 2:
        return np.array([])
    dp = np.diff(closes)                       # length n-1, aligns to bars[1:]
    sigma = np.std(dp, ddof=1)
    if sigma <= 0:
        return np.array([])
    buyfrac = _phi(dp / sigma)                  # in (0, 1)
    imbalance = np.abs(2.0 * buyfrac - 1.0)     # |V_buy - V_sell| / V per bar

    vol = np.asarray(volumes, dtype=float)[1:] if volumes is not None else None
    if vol is None or vol.size != imbalance.size or float(np.nansum(vol)) <= 0:
        vol = np.ones_like(imbalance)           # equal-weight fallback

    num = vol * imbalance
    out = []
    for i in range(window - 1, imbalance.size):
        v = vol[i - window + 1: i + 1]
        s = num[i - window + 1: i + 1]
        denom = float(np.sum(v))
        out.append(float(np.sum(s) / denom) if denom > 0 else 0.0)
    return np.asarray(out)


def toxicity_read(closes, volumes) -> tuple[float, float]:
    """Latest BVC-VPIN and its percentile within the symbol's own VPIN history.
    Returns (0.0, 0.0) when there is not enough history."""
    window = max(2, CONFIG.vpin_window_bars)
    series = bvc_vpin_series(closes, volumes, window)
    if series.size < 3:
        return 0.0, 0.0
    latest = float(series[-1])
    pct = float(np.mean(series <= latest))      # percentile rank of latest
    return latest, pct


class FlowRiskManager:
    """Produces a FlowRead from a bars() payload. Stateless; safe to share."""

    def assess(self, bars: dict) -> FlowRead:
        closes = bars.get("close") or []
        volumes = bars.get("volume")
        if len(closes) < 5:
            return FlowRead()

        read = FlowRead()

        # A10 -- vol-target sizing multiplier
        if CONFIG.vol_sizing_enabled:
            read.vol_mult, read.vol_basis = vol_target_multiplier(closes)

        # A8 -- toxicity veto
        if CONFIG.toxicity_veto_enabled:
            tox, pct = toxicity_read(closes, volumes)
            read.toxicity, read.tox_pct = tox, pct
            enough = len(closes) >= CONFIG.toxicity_min_bars
            if enough and pct >= CONFIG.toxicity_pct_threshold:
                read.veto = True
                read.veto_reason = (
                    f"flow toxicity VPIN={tox:.2f} at pct {pct:.0%} "
                    f">= {CONFIG.toxicity_pct_threshold:.0%} (stand aside)"
                )
        return read
