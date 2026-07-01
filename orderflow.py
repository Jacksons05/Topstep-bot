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
import threading
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
DEPTH_LEVELS       = int(_f("OF_DEPTH_LEVELS", 5))   # ladder levels summed for depth OBI
# Staleness window: a quote/trade older than this (wall-clock arrival) makes the
# book report has_data=False (fail closed) — a frozen feed must NOT pass the gate.
STALENESS_SEC      = _f("OF_STALENESS_SEC", 5.0)


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


class MultiLevelBook:
    """Market-by-price depth ladder (CME L2 / Rithmic ORDER_BOOK template 156).

    Holds N price levels per side as {price: size}. Fed by depth snapshots +
    incremental updates from the live feed. Top-of-book still works without it
    (the engine falls back to BBO sizes); when populated, OBI/footprint use the
    summed depth across the top `levels`, which is the real microstructure the
    strategy wants (not just best bid/ask).
    """

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}   # price -> resting size
        self.asks: dict[float, float] = {}
        self._has = False

    @property
    def has_depth(self) -> bool:
        return self._has and bool(self.bids) and bool(self.asks)

    def apply_snapshot(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        self.bids = {float(p): float(s) for p, s in bids if s and s > 0}
        self.asks = {float(p): float(s) for p, s in asks if s and s > 0}
        self._has = True

    def apply_update(self, side: str, price: float, size: float) -> None:
        """Incremental level update; size 0 removes the level."""
        book = self.bids if side == "bid" else self.asks
        if size and size > 0:
            book[float(price)] = float(size)
        else:
            book.pop(float(price), None)
        self._has = True

    def top_bids(self, n: int) -> list[tuple[float, float]]:
        return sorted(self.bids.items(), key=lambda kv: -kv[0])[:n]

    def top_asks(self, n: int) -> list[tuple[float, float]]:
        return sorted(self.asks.items(), key=lambda kv: kv[0])[:n]

    def depth_obi(self, levels: int) -> float:
        """Imbalance summed over the top `levels` of each side: (ΣLb−ΣLa)/(ΣLb+ΣLa)."""
        lb = sum(s for _, s in self.top_bids(levels))
        la = sum(s for _, s in self.top_asks(levels))
        tot = lb + la
        return (lb - la) / tot if tot > 0 else 0.0

    def best_bid(self) -> tuple[float, float] | None:
        b = self.top_bids(1)
        return b[0] if b else None

    def best_ask(self) -> tuple[float, float] | None:
        a = self.top_asks(1)
        return a[0] if a else None


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
        # Wall-clock arrival timestamps of the most recent quote / trade. Drive the
        # has_data freshness check (a reconnect-frozen book latches stale, not True).
        self.last_quote_ts = 0.0
        self.last_trade_ts = 0.0
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
        self.book = MultiLevelBook()          # L2 ladder when ORDER_BOOK feed is live
        self.depth_levels = DEPTH_LEVELS
        self._lock = threading.Lock()         # guards _buckets + _trail (writer=websocket, reader=engine)

    @property
    def _last_update_ts(self) -> float:
        return max(self.last_quote_ts, self.last_trade_ts)

    @property
    def ever_had_data(self) -> bool:
        """True once ANY quote/trade has ever arrived for this symbol."""
        return self._last_update_ts > 0.0

    @property
    def has_data(self) -> bool:
        """True only when a FRESH (within STALENESS_SEC) quote/trade backs a real
        book. A cold feed (never warmed) returns False → the engine fails OPEN
        during warm-up; a frozen/reconnected feed also returns False but is
        reported `stale` so the engine fails CLOSED on stale microstructure."""
        if not self.ever_had_data:
            return False
        if (time.time() - self._last_update_ts) > STALENESS_SEC:
            return False
        return (self.bid_size + self.ask_size) > 0 or self._trades_seen > 0 or self.book.has_depth

    @property
    def stale(self) -> bool:
        """True when the feed HAD data but the latest update is older than the
        staleness window (a frozen book). Distinguishes a stalled feed from a
        never-warmed one so the gate can fail closed only on the former."""
        return self.ever_had_data and (time.time() - self._last_update_ts) > STALENESS_SEC

    # ── L2 depth ingest (Rithmic ORDER_BOOK / template 156) ──
    def on_depth_snapshot(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        self.book.apply_snapshot(bids, asks)
        self.last_quote_ts = time.time()
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if bb and ba:                          # keep top-of-book mirror in sync
            self.bid, self.bid_size = bb
            self.ask, self.ask_size = ba

    def on_depth_update(self, side: str, price: float, size: float) -> None:
        self.book.apply_update(side, price, size)
        self.last_quote_ts = time.time()
        bb, ba = self.book.best_bid(), self.book.best_ask()
        if bb:
            self.bid, self.bid_size = bb
        if ba:
            self.ask, self.ask_size = ba

    # ── ingest ────────────────────────────────────────────
    def on_depth(self, bid: float, bid_size: float, ask: float, ask_size: float) -> None:
        self.bid, self.bid_size, self.ask, self.ask_size = bid, bid_size, ask, ask_size
        self.last_quote_ts = time.time()

    def on_trade(self, price: float, size: float, ts: float | None = None) -> int:
        """Classify aggressor vs NBBO, update CVD + whale buckets.

        Returns the signed direction of the trade (+1 buy, -1 sell, 0 unknown).
        """
        ts = ts if ts is not None else time.time()
        # Staleness is keyed off real arrival time (NOT the possibly-synthetic
        # exchange ts used for the 1-sec notional buckets below).
        self.last_trade_ts = time.time()
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
        with self._lock:
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
        # Prefer multi-level depth imbalance (summed over top N ladder levels)
        # when the L2 book is live; fall back to top-of-book BBO sizes.
        if self.book.has_depth:
            return self.book.depth_obi(self.depth_levels)
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
        with self._lock:
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
        with self._lock:
            if len(self._trail) < 10:
                return ""
            trail_snap = list(self._trail)
        prices = [p for p, _ in trail_snap]
        cvds = [c for _, c in trail_snap]
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
        with self._lock:
            self._buckets.clear()
            self._trail.clear()
        self._last_whale = 0
