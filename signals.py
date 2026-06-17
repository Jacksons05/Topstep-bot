"""Signal type + the deterministic quantitative stream.

The quant stream is one half of the confluence: it reads technical structure
(SMA cross, RSI, ATR) from a bar series and emits a directional lean with a
0..1 strength. The qualitative (LLM) stream lives in agents.py. A trade only
fires when both agree (see engine.py).

Random Forest on engineered features is the documented upgrade path; this
ships a transparent indicator model so the pipeline runs with zero ML deps and
every decision is inspectable. Swap `quant_signal` for a trained model later.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import CONFIG

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Signal:
    symbol: str
    asset: str               # "equity" | "option"
    side: str                # "BUY" | "SELL"
    price: float             # reference / limit price
    confidence: float        # 0..1 combined confluence score
    kind: str = "confluence"  # confluence | arb | gex | manual
    confidence_label: str = "medium"
    thesis: str = ""
    # provenance — which streams contributed
    quant: float = 0.0       # -1..1 quant lean
    qual: float = 0.0        # -1..1 qualitative lean
    atr: float = 0.0         # for ATR-based stop/target sizing
    stop: float = 0.0        # explicit stop (options: gamma flip); equity uses ATR instead
    agents: dict = field(default_factory=dict)  # {"analyst": True, "risk": True, ...}
    # options leg (only when asset == "option")
    contract: str = ""       # OCC symbol, e.g. AAPL250117C00150000
    structure: object = None  # options_strategy.OptionStructure at runtime (not persisted)

    @property
    def meets_min_confidence(self) -> bool:
        return _CONF_RANK[self.confidence_label] >= _CONF_RANK[CONFIG.min_confidence]


def label_for(conf: float) -> str:
    if conf >= 0.75:
        return "high"
    if conf >= 0.55:
        return "medium"
    return "low"


# ── technical indicators (no numpy needed) ────────────────

def sma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs) / period


# ── quantitative stream ───────────────────────────────────

@dataclass
class QuantRead:
    lean: float          # -1 (bearish) .. +1 (bullish)
    strength: float      # 0..1 magnitude of conviction
    atr: float
    detail: str

    @property
    def direction(self) -> str:
        return "BUY" if self.lean > 0 else ("SELL" if self.lean < 0 else "FLAT")


def quant_signal(bars: dict) -> QuantRead | None:
    """Indicator confluence from OHLC bars.

    `bars` = {"close": [...], "high": [...], "low": [...]} oldest->newest.
    Combines an SMA trend filter with an RSI mean-reversion read. Returns None
    when there aren't enough bars to compute the indicators.
    """
    closes = bars.get("close") or []
    highs = bars.get("high") or closes
    lows = bars.get("low") or closes
    if len(closes) < CONFIG.sma_slow + 1:
        return None

    fast = sma(closes, CONFIG.sma_fast)
    slow = sma(closes, CONFIG.sma_slow)
    r = rsi(closes, CONFIG.rsi_period)
    a = atr(highs, lows, closes, CONFIG.atr_period) or 0.0
    if fast is None or slow is None or r is None:
        return None

    lean = 0.0
    bits = []
    # Trend: fast over slow = bullish bias.
    trend = (fast - slow) / slow if slow else 0.0
    if trend > 0:
        lean += 0.5
        bits.append(f"SMA{CONFIG.sma_fast}>{CONFIG.sma_slow} (+{trend*100:.1f}%)")
    elif trend < 0:
        lean -= 0.5
        bits.append(f"SMA{CONFIG.sma_fast}<{CONFIG.sma_slow} ({trend*100:.1f}%)")

    # RSI: oversold in an uptrend = add long; overbought = fade.
    if r <= CONFIG.rsi_oversold:
        lean += 0.5
        bits.append(f"RSI {r:.0f} oversold")
    elif r >= CONFIG.rsi_overbought:
        lean -= 0.5
        bits.append(f"RSI {r:.0f} overbought")
    else:
        bits.append(f"RSI {r:.0f}")

    lean = max(-1.0, min(1.0, lean))
    return QuantRead(lean=lean, strength=abs(lean), atr=a, detail=" · ".join(bits))
