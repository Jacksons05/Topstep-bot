"""LightGBM quant signal — drop-in replacement for signals.quant_signal.

Produces the SAME `QuantRead` (lean, strength, atr, detail) the engine already
consumes, so engine.py / backtest.py keep working unchanged: the only call-site
change is "try ML first, fall back to the indicator model" (see engine._prescreen).

Design choices that keep it safe:
  * LAZY, OPTIONAL deps. lightgbm is imported only when a model is actually
    loaded. No model file or no lightgbm installed → `ready` is False and the
    engine transparently falls back to signals.quant_signal. Nothing breaks.
  * Calibrated probability → confidence. The model predicts P(up); lean =
    2·P − 1 ∈ [−1, 1] and strength = |lean|. That flows straight into the
    existing fractional-Kelly sizer (risk.kelly_fraction wants a probability).
  * A min-probability deadband (CONFIG.ml_min_prob) maps a weak edge to FLAT so
    the confluence gate doesn't fire on coin-flips.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from config import CONFIG
from features import FEATURE_NAMES, feature_row, row_to_vector
from signals import QuantRead, atr as _atr_list

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent


class MLQuant:
    """Lazy-loaded LightGBM model that emits a QuantRead. Singleton `ML` below."""

    def __init__(self, model_path: str | None = None, features_path: str | None = None):
        self._model_path = ROOT / (model_path or CONFIG.ml_model_path)
        self._features_path = ROOT / (features_path or CONFIG.ml_features_path)
        self._booster = None
        self._feature_names: tuple[str, ...] = FEATURE_NAMES
        self._loaded = False
        self._load_failed = False

    # ── loading ───────────────────────────────────────────────────────────
    def _ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        if self._load_failed or not self._model_path.exists():
            return False
        try:
            import lightgbm as lgb  # lazy: only needed when a model exists
        except ImportError:
            log.warning("[ML] lightgbm not installed — falling back to quant_signal. "
                        "pip install lightgbm to enable the ML signal.")
            self._load_failed = True
            return False
        try:
            self._booster = lgb.Booster(model_file=str(self._model_path))
            if self._features_path.exists():
                names = json.loads(self._features_path.read_text())
                self._feature_names = tuple(names)
                if self._feature_names != FEATURE_NAMES:
                    log.warning("[ML] model feature order differs from features.FEATURE_NAMES "
                                "— retrain after changing the feature set.")
            self._loaded = True
            log.info(f"[ML] model loaded from {self._model_path.name} "
                     f"({len(self._feature_names)} features)")
            return True
        except Exception as e:  # noqa: BLE001
            log.error(f"[ML] failed to load model: {e} — falling back to quant_signal")
            self._load_failed = True
            return False

    @property
    def ready(self) -> bool:
        """True when a usable model is loaded. Engine checks this before use."""
        return self._ensure_loaded()

    # ── inference ──────────────────────────────────────────────────────────
    def read(self, bars: dict, micro: dict | None = None) -> QuantRead | None:
        """Predict from a bar buffer (+ optional live order-flow dict). Returns a
        QuantRead, or None to signal the caller to fall back to quant_signal
        (model not ready, too few bars, or sub-threshold edge → no opinion)."""
        if not self.ready:
            return None
        row = feature_row(bars, micro)
        if row is None:
            return None
        vec = row_to_vector(row).reshape(1, -1)
        try:
            p_up = float(self._booster.predict(vec)[0])
        except Exception as e:  # noqa: BLE001
            log.error(f"[ML] predict failed: {e} — falling back")
            return None

        lean = 2.0 * p_up - 1.0
        # deadband: weak edge → no signal, let confluence stay flat
        if abs(p_up - 0.5) < (CONFIG.ml_min_prob - 0.5):
            lean = 0.0
        lean = max(-1.0, min(1.0, lean))

        closes = bars.get("close") or []
        highs = bars.get("high") or closes
        lows = bars.get("low") or closes
        a = _atr_list(highs, lows, closes, CONFIG.atr_period) or row.get("atr_pct", 0.0) * (closes[-1] if closes else 0.0)
        micro_note = ""
        if micro and micro.get("obi") is not None:
            micro_note = f" · OBI {float(micro['obi']):+.2f}"
        detail = f"ML P(up)={p_up:.2f}{micro_note}"
        return QuantRead(lean=lean, strength=abs(lean), atr=float(a), detail=detail)

    def win_prob(self, bars: dict, micro: dict | None = None) -> float | None:
        """Raw P(up) for sizing/diagnostics (None when not ready)."""
        if not self.ready:
            return None
        row = feature_row(bars, micro)
        if row is None:
            return None
        return float(self._booster.predict(row_to_vector(row).reshape(1, -1))[0])


# module singleton — cheap until a model file exists (lazy load)
ML = MLQuant()
