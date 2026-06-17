"""Live Rithmic L1/L2/trade feed → per-symbol OrderFlowEngine.

Reuses the RithmicBroker's already-connected RithmicClient and background event
loop (one persistent socket, per the research's "centralized hub" note) to
stream, for each traded futures root:
  • BBO        (DataType.BBO)        → top-of-book sizes (OBI / micro-price)
  • LAST_TRADE (DataType.LAST_TRADE) → trade prints (CVD / whale flag)
  • ORDER_BOOK (DataType.ORDER_BOOK) → full L2 depth ladder (multi-level OBI)

async-rithmic ≥1.6 surfaces L2 natively: subscribe_to_market_data(..., ORDER_BOOK)
streams the OrderBook message (template 156) to `client.on_order_book`, carrying
repeated bid_price[]/bid_size[] and ask_price[]/ask_size[] arrays (the ladder)
plus an update_type. The CME Level-2 data subscription must be enabled on the
account for these to flow (non-pro for funded traders).

Degrades safely: in mock mode (no async-rithmic / no creds) it subscribes to
nothing and every engine stays empty → has_data False → the gate fails open.
"""
from __future__ import annotations

import logging

from config import CONFIG
from futures_symbols import spec_for
from orderflow import OrderFlowEngine

log = logging.getLogger(__name__)

try:
    from async_rithmic import DataType
    _DT_BBO = DataType.BBO
    _DT_LAST = DataType.LAST_TRADE
    _DT_ORDER_BOOK = DataType.ORDER_BOOK      # native L2 in async-rithmic ≥1.6
except Exception:  # noqa: BLE001 - library absent → mock path only
    _DT_BBO = _DT_LAST = _DT_ORDER_BOOK = None


def _ladder_from_orderbook(msg) -> tuple[list, list]:
    """Map a Rithmic OrderBook protobuf (template 156) → (bids, asks) level lists.

    bid_price/bid_size and ask_price/ask_size are repeated scalar fields (the
    ladder, top level first). Empty arrays (e.g. update_type NO_BOOK) yield empty
    lists, which the book treats as "no depth".
    """
    bp = list(getattr(msg, "bid_price", []) or [])
    bs = list(getattr(msg, "bid_size", []) or [])
    ap = list(getattr(msg, "ask_price", []) or [])
    asz = list(getattr(msg, "ask_size", []) or [])
    bids = [(float(p), float(z)) for p, z in zip(bp, bs)]
    asks = [(float(p), float(z)) for p, z in zip(ap, asz)]
    return bids, asks


class RithmicOrderFlowFeed:
    """Subscribes Rithmic market data and routes it into per-symbol engines."""

    def __init__(self, broker) -> None:
        # broker is a RithmicBroker; reuse its client + background loop.
        self._broker = broker
        self._client = getattr(broker, "_client", None)
        self._mock = getattr(broker, "_mock_mode", True)
        self._engines: dict[str, OrderFlowEngine] = {}
        self._subscribed: set[str] = set()
        self._handlers_registered = False
        # L2 is native when the library exposes DataType.ORDER_BOOK (async-rithmic ≥1.6).
        self.depth_available = _DT_ORDER_BOOK is not None and not self._mock

    def get(self, symbol: str) -> OrderFlowEngine:
        """Return (creating if needed) the engine for a futures root."""
        sym = symbol.upper()
        eng = self._engines.get(sym)
        if eng is None:
            spec = spec_for(sym)
            mult = spec.multiplier if spec else 1.0
            eng = OrderFlowEngine(multiplier=mult)
            self._engines[sym] = eng
        return eng

    def subscribe(self, symbols: list[str]) -> int:
        """Subscribe BBO + LAST_TRADE (+ ORDER_BOOK) for every futures root.

        Non-futures symbols (no FUTURES_SPECS entry) are skipped — order flow is
        a futures-only signal here. Returns the count actually subscribed.
        """
        if self._mock or self._client is None or _DT_BBO is None:
            log.info("[OrderFlow] mock/unavailable — no live subscription; gate fails open")
            return 0

        self._register_handlers()
        log.info(
            "[OrderFlow] L2 depth %s",
            "ACTIVE (ORDER_BOOK → multi-level OBI). Needs the account's CME "
            "Level-2 data subscription enabled." if self.depth_available
            else "OFF — library lacks DataType.ORDER_BOOK (upgrade async-rithmic ≥1.6).",
        )
        n = 0
        for symbol in symbols:
            sym = symbol.upper()
            spec = spec_for(sym)
            if spec is None or sym in self._subscribed:
                continue
            self.get(sym)  # ensure engine exists before ticks arrive
            try:
                self._broker._run(
                    self._client.subscribe_to_market_data(sym, spec.exchange, _DT_BBO)
                )
                self._broker._run(
                    self._client.subscribe_to_market_data(sym, spec.exchange, _DT_LAST)
                )
                feeds = "BBO + LAST_TRADE"
                if self.depth_available:
                    self._broker._run(
                        self._client.subscribe_to_market_data(sym, spec.exchange, _DT_ORDER_BOOK)
                    )
                    feeds += " + ORDER_BOOK(L2)"
                self._subscribed.add(sym)
                n += 1
                log.info(f"[OrderFlow] subscribed {sym} @ {spec.exchange} ({feeds})")
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[OrderFlow] subscribe failed for {sym}: {exc}")
        return n

    def _register_handlers(self) -> None:
        if self._handlers_registered or self._client is None:
            return

        async def _on_tick(data) -> None:
            try:
                sym = (data.get("symbol") or "").upper()
                if not sym or sym not in self._engines:
                    return
                eng = self._engines[sym]
                dtype = data.get("data_type")
                if dtype == _DT_BBO:
                    eng.on_depth(
                        bid=float(data.get("bid_price", 0) or 0),
                        bid_size=float(data.get("bid_size", 0) or 0),
                        ask=float(data.get("ask_price", 0) or 0),
                        ask_size=float(data.get("ask_size", 0) or 0),
                    )
                elif dtype == _DT_LAST:
                    price = float(data.get("trade_price", 0) or 0)
                    size = float(data.get("trade_size", 0) or 0)
                    if price > 0 and size > 0:
                        ssboe = data.get("ssboe")
                        eng.on_trade(price, size, ts=float(ssboe) if ssboe else None)
            except Exception as exc:  # noqa: BLE001 - never let a bad tick kill the loop
                log.debug(f"[OrderFlow] tick handler error: {exc}")

        async def _on_order_book(msg) -> None:
            try:
                sym = (getattr(msg, "symbol", "") or "").upper()
                if not sym or sym not in self._engines:
                    return
                bids, asks = _ladder_from_orderbook(msg)
                if bids and asks:                     # NO_BOOK / empty → leave book as-is
                    self._engines[sym].on_depth_snapshot(bids, asks)
            except Exception as exc:  # noqa: BLE001
                log.debug(f"[OrderFlow] order-book handler error: {exc}")

        self._client.on_tick += _on_tick
        if self.depth_available:
            self._client.on_order_book += _on_order_book
        self._handlers_registered = True

    def reset_session(self) -> None:
        """Zero every engine's CVD at the open."""
        for eng in self._engines.values():
            eng.reset_session()
