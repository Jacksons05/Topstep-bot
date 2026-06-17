"""Order-flow / microstructure confirmation for the dealer-gamma breakout.

Implements the metrics the strategy research (docs-orderflow.md) names as
*required* entry triggers at gamma walls and the flip: order-book imbalance
(OBI), micro-price, cumulative volume delta (CVD) with divergence/absorption,
and a MAD-based modified z-score "whale" flag for institutional block trades.

Pure stdlib so it's testable offline with synthetic depth/trades. A live
adapter feeds `on_depth()` / `on_trade()` from the Rithmic L2 stream
(async-rithmic); `confirm_entry()` returns a gate the engine applies alongside
the GEX level and the Risk-Manager agent veto.

Thresholds (overridable via env, defaults from the sources):
    OF_OBI_THRESHOLD       0.85   OBI magnitude required at a wall (→ ±1.0)
    OF_WHALE_Z             3.5    MAD modified z-score flag
    OF_WHALE_NOTIONAL_USD  1e6    min $ in a 1-second bucket to be a "whale"
    OF_FOOTPRINT_RATIO     10.0   aggressive-side dominance for absorption
    OF_WINDOW_SEC          120    rolling window for z-score / divergence
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from statistics import median


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


OBI_THRESHOLD      = _f("OF_OBI_THRESHOLD", 0.85)
WHALE_Z            = _f("OF_WHALE_Z", 3.5)
WHALE_NOTIONAL_USD = _f("OF_WHALE_NOTIONAL_USD", 1_000_000.0)
FOOTPRINT_RATIO    = _f("OF_FOOTPRINT_RATIO", 10.0)
WINDOW_SEC         = int(_f("OF_WINDOW_SEC", 120))


def mad_modified_z(x: float, series: list[float]) -> float:
    """Median-Absolute-Deviation modified z-score (Iglewicz–Hoaglin).

        Mz = 0.6745 * (x - median) / MAD,   MAD = median(|xi - median|)

    Robust to the outliers a single whale print creates. Returns 0.0 when the
    series is too short or MAD is zero (degenerate → no flag).
    """
    if len(series) < 3:
        return 0.0
    med = median(series)
    mad = median([abs(v - med) for v in series])
    if mad == 0:
        return 0.0
    return 0.6745 * (x - med) / mad


@dataclass
class OrderFlowSnapshot:
    obi: float            # -1..+1, + = bid-heavy (buy pressure)
    micro_price: float
    cvd: float            # cumulative volume delta since reset
    whale: int            # -1 sell-side whale, +1 buy-side, 0 none
    ts: float


class OrderFlowEngine:
    """Streaming order-flow state. Feed it depth + trades; read the metrics."""

    def __init__(self, window_sec: int = WINDOW_SEC, multiplier: float = 1.0):
        # latest top-of-book
        self.bid = 0.0
        self.ask = 0.0
        self.bid_size = 0.0
        self.ask_size = 0.0
        self.multiplier = multiplier          # $ per point (futures contract spec)
        self._window = window_sec
        self.cvd = 0.0
        self._last_price = 0.0
        # per-second aggressor-signed notional buckets: {epoch_sec: signed_usd}
        self._buckets: "deque[tuple[int, float]]" = deque()
        # (price, cvd) trail for divergence detection
        self._trail: "deque[tuple[float, float]]" = deque()
        self._last_whale = 0
        self._trades_seen = 0

    @property
    def has_data(self) -> bool:
        """True once real depth or trades have arrived. The engine only applies
        the order-flow gate when this is True, so a cold feed fails OPEN rather
        than blocking every entry during warm-up."""
        return (self.bid_size + self.ask_size) > 0 or self._trades_seen > 0

    # ── ingest ────────────────────────────────────────────
    def on_depth(self, bid: float, bid_size: float, ask: float, ask_size: float) -> None:
        self.bid, self.bid_size, self.ask, self.ask_size = bid, bid_size, ask, ask_size

    def on_trade(self, price: float, size: float, ts: float | None = None) -> int:
        """Classify aggressor vs NBBO, update CVD + whale buckets.

        Returns the signed direction of the trade (+1 buy, -1 sell, 0 unknown).
        """
        ts = ts if ts is not None else time.time()
        # aggressor: at/above ask = buyer-initiated, at/below bid = seller-initiated;
        # between → tick rule vs last print.
        if self.ask and price >= self.ask:
            side = 1
        elif self.bid and price <= self.bid:
            side = -1
        elif self._last_price:
            side = 1 if price > self._last_price else (-1 if price < self._last_price else 0)
        else:
            side = 0
        self._last_price = price
        self._trades_seen += 1

        delta = side * size
        self.cvd += delta
        notional = price * size * self.multiplier * side

        sec = int(ts)
        if self._buckets and self._buckets[-1][0] == sec:
            self._buckets[-1] = (sec, self._buckets[-1][1] + notional)
        else:
            self._buckets.append((sec, notional))
        self._evict(ts)
        self._trail.append((price, self.cvd))
        while len(self._trail) > 5000:
            self._trail.popleft()
        return side

    def _evict(self, now: float) -> None:
        cutoff = int(now) - self._window
        while self._buckets and self._buckets[0][0] < cutoff:
            self._buckets.popleft()

    # ── metrics ───────────────────────────────────────────
    @property
    def obi(self) -> float:
        tot = self.bid_size + self.ask_size
        return (self.bid_size - self.ask_size) / tot if tot > 0 else 0.0

    @property
    def micro_price(self) -> float:
        # liquidity-weighted mid: each side weighted by the OPPOSITE-side size.
        tot = self.bid_size + self.ask_size
        if tot <= 0:
            return (self.bid + self.ask) / 2 if (self.bid and self.ask) else 0.0
        return (self.ask * self.bid_size + self.bid * self.ask_size) / tot

    def whale(self) -> int:
        """+1 / -1 if the latest 1-sec bucket is a statistically large block on
        the buy / sell side (MAD z > WHALE_Z and |notional| ≥ WHALE_NOTIONAL_USD),
        else 0."""
        if len(self._buckets) < 3:
            return 0
        sec, signed = self._buckets[-1]
        mags = [abs(v) for _, v in self._buckets]
        z = mad_modified_z(abs(signed), mags)
        if z > WHALE_Z and abs(signed) >= WHALE_NOTIONAL_USD:
            self._last_whale = 1 if signed > 0 else -1
            return self._last_whale
        return 0

    def cvd_divergence(self) -> str:
        """Detect price/CVD divergence over the trail.

        'bearish' = price makes a new high while CVD fails to (buying exhaustion);
        'bullish' = price makes a new low while CVD turns up (seller exhaustion);
        '' = none.
        """
        if len(self._trail) < 10:
            return ""
        prices = [p for p, _ in self._trail]
        cvds = [c for _, c in self._trail]
        cur_p, cur_c = prices[-1], cvds[-1]
        prior_p = prices[:-1]
        prior_c = cvds[:-1]
        if cur_p >= max(prior_p) and cur_c < max(prior_c):
            return "bearish"
        if cur_p <= min(prior_p) and cur_c > min(prior_c):
            return "bullish"
        return ""

    # ── the gate the engine calls ─────────────────────────
    def confirm_entry(self, direction: str, near_wall: bool = True) -> tuple[bool, str]:
        """Order-flow confirmation for a GEX entry at a wall/flip.

        direction : "BUY" or "SELL" (the structural lean from GEX).
        Requires extreme OBI in the trade direction; a same-side whale flag or a
        confirming CVD read strengthens it. Returns (ok, reason).
        """
        want = 1 if direction == "BUY" else -1
        obi = self.obi
        obi_ok = (obi >= OBI_THRESHOLD) if want > 0 else (obi <= -OBI_THRESHOLD)
        if not (near_wall and obi_ok):
            return False, f"OBI {obi:+.2f} not extreme for {direction} (need {'≥' if want>0 else '≤'}{want*OBI_THRESHOLD:+.2f})"

        wh = self.whale()
        div = self.cvd_divergence()
        bits = [f"OBI {obi:+.2f}"]
        if wh == want:
            bits.append("whale-confirm")
        elif wh == -want:
            return False, f"OBI ok but opposing whale ({wh:+d}) at wall"
        # CVD divergence opposing the trade = exhaustion against us → veto.
        if (direction == "BUY" and div == "bearish") or (direction == "SELL" and div == "bullish"):
            return False, f"OBI ok but CVD {div} divergence vs {direction}"
        if div:
            bits.append(f"cvd:{div}")
        bits.append(f"cvd={self.cvd:+.0f}")
        return True, " ".join(bits)

    def snapshot(self) -> OrderFlowSnapshot:
        return OrderFlowSnapshot(
            obi=round(self.obi, 4), micro_price=round(self.micro_price, 4),
            cvd=self.cvd, whale=self.whale(), ts=time.time(),
        )

    def reset_session(self) -> None:
        """Zero CVD + buckets at the open (CVD is a since-open running total)."""
        self.cvd = 0.0
        self._buckets.clear()
        self._trail.clear()
        self._last_whale = 0
