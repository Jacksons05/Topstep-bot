"""Live Rithmic L1/trade feed → per-symbol OrderFlowEngine.

Reuses the RithmicBroker's already-connected RithmicClient and background event
loop (one persistent socket, per the research's "centralized hub" note) to
stream Best-Bid-Offer (top-of-book sizes → OBI / micro-price) and Last-Trade
prints (→ CVD / whale flag) for the traded futures roots. The engine reads the
resulting OrderFlowEngine via `confirm_entry()` as a final entry gate.

async-rithmic market-data surface (verified, v1.2.7):
  await client.subscribe_to_market_data(symbol, exchange, DataType.BBO)
  await client.subscribe_to_market_data(symbol, exchange, DataType.LAST_TRADE)
  client.on_tick += handler        # handler(data: dict)
    BBO  (data_type=DataType.BBO):        bid_price, bid_size, ask_price, ask_size
    LAST (data_type=DataType.LAST_TRADE): trade_price, trade_size, aggressor, ssboe

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
except Exception:  # noqa: BLE001 - library absent → mock path only
    _DT_BBO = _DT_LAST = None


class _OrderBookBits:
    """subscribe_to_market_data() only reads `.value` as the Rithmic update_bits
    bitmask. The protocol defines ORDER_BOOK = 4 (full depth ladder), but
    async-rithmic's DataType enum only exposes BBO(2)/LAST_TRADE(1) — so we pass
    this shim to request the depth stream the account is entitled to."""
    value = 4
    name = "ORDER_BOOK"


_DT_ORDER_BOOK = _OrderBookBits()


def _register_depth_template() -> bool:
    """Wire Rithmic's OrderBook protobuf (inbound template 156) into the plant's
    decode map if it's available. async-rithmic ships the depth references
    COMMENTED OUT and does not bundle order_book_pb2, so this returns False until
    the R|Protocol order_book.proto is compiled in. The rest of the depth path is
    ready; only this decode is gated on that file.
    """
    try:
        from async_rithmic.plants.base import TEMPLATES_MAP
        from async_rithmic.protocol_buffers import order_book_pb2  # not bundled (yet)
        TEMPLATES_MAP.setdefault(156, order_book_pb2.OrderBook)
        return True
    except Exception:  # noqa: BLE001 - proto not present → depth decode unavailable
        return False


def _depth_levels_from_msg(data: dict) -> tuple[list, list] | None:
    """Map a decoded OrderBook dict → (bids, asks) level lists.

    Rithmic's OrderBook (template 156) carries parallel arrays bid_price[]/
    bid_size[] and ask_price[]/ask_size[]. Field names are confirmed against a
    live tick before this is relied on; returns None when the shape isn't depth.
    """
    bp, bs = data.get("bid_price"), data.get("bid_size")
    ap, asz = data.get("ask_price"), data.get("ask_size")
    if not (isinstance(bp, (list, tuple)) and isinstance(ap, (list, tuple))):
        return None
    bids = [(float(p), float(z)) for p, z in zip(bp, bs or [])]
    asks = [(float(p), float(z)) for p, z in zip(ap, asz or [])]
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
        self._handler_registered = False
        # Try to wire the L2 depth decode (template 156). When unavailable the
        # feed still streams top-of-book BBO + trades; only multi-level OBI is off.
        self.depth_active = _register_depth_template() if not self._mock else False

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
        """Subscribe BBO + LAST_TRADE for every futures root in `symbols`.

        Non-futures symbols (no FUTURES_SPECS entry) are skipped — order flow is
        a futures-only signal here. Returns the count actually subscribed.
        """
        if self._mock or self._client is None or _DT_BBO is None:
            log.info("[OrderFlow] mock/unavailable — no live subscription; gate fails open")
            return 0

        self._register_handler()
        log.info(
            "[OrderFlow] L2 depth %s",
            "ACTIVE (ORDER_BOOK template 156 → multi-level OBI)" if self.depth_active
            else "OFF — order_book proto not registered; top-of-book BBO only. "
                 "Add Rithmic R|Protocol order_book.proto to enable.",
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
                if self.depth_active:
                    # Full CME L2 depth (the account is entitled to it) — multi-level OBI.
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

    def _register_handler(self) -> None:
        if self._handler_registered or self._client is None:
            return

        async def _on_tick(data) -> None:
            try:
                sym = (data.get("symbol") or "").upper()
                if not sym or sym not in self._engines:
                    return
                eng = self._engines[sym]
                dtype = data.get("data_type")
                # L2 depth (template 156): parallel bid/ask price+size arrays.
                # Routed first since its dict also carries bid_price as a list.
                levels = _depth_levels_from_msg(data)
                if levels is not None:
                    eng.on_depth_snapshot(levels[0], levels[1])
                    return
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

        self._client.on_tick += _on_tick
        self._handler_registered = True

    def reset_session(self) -> None:
        """Zero every engine's CVD at the open."""
        for eng in self._engines.values():
            eng.reset_session()
