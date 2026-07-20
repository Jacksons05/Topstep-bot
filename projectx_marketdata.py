"""Live ProjectX (TopstepX) market feed -> per-symbol OrderFlowEngine.

Connects the ProjectX real-time **market hub** (SignalR, rtc.topstepx.com) and
streams, for each traded futures root:
  • GatewayQuote  (SubscribeContractQuotes) → best bid/ask (+sizes) for OBI/micro-price
  • GatewayTrade  (SubscribeContractTrades) → trade prints for CVD / whale flag
  • GatewayDepth  (SubscribeContractMarketDepth) → full DOM ladder (multi-level OBI)

This replaces rithmic_marketdata.RithmicOrderFlowFeed. The OrderFlowEngine and
the engine's confirm_entry() gate are unchanged — only the transport differs.

Auth: the access token is taken from the already-authenticated ProjectXBroker
and passed as the `access_token` query param on the hub URL.

DOM (GatewayDepth) entries carry a DomType:
    1 Ask  2 Bid  3 BestAsk  4 BestBid  5 Trade  6 Reset
    7 Low  8 High  9 NewBestBid  10 NewBestAsk  11 Fill
We map {2,4,9} → bid side and {1,3,10} → ask side; volume 0 removes the level.

Degrades safely: if signalrcore is not installed or the broker is in mock mode,
it subscribes to nothing and every engine stays empty → has_data False → the
order-flow gate fails open.
"""
from __future__ import annotations

import logging
import time

from config import CONFIG
from futures_symbols import spec_for
from orderflow import OrderFlowEngine

log = logging.getLogger(__name__)

# ── Optional import: signalrcore (SignalR ASP.NET Core client) ────────────────
try:
    from signalrcore.hub_connection_builder import HubConnectionBuilder
    _SIGNALR_AVAILABLE = True
except Exception:  # noqa: BLE001 - library absent → mock path only
    HubConnectionBuilder = None  # type: ignore[assignment]
    _SIGNALR_AVAILABLE = False

# DomType → book side
_BID_TYPES = {2, 4, 9}   # Bid, BestBid, NewBestBid
_ASK_TYPES = {1, 3, 10}  # Ask, BestAsk, NewBestAsk


class ProjectXOrderFlowFeed:
    """Subscribes ProjectX market data and routes it into per-symbol engines."""

    def __init__(self, broker, on_boundary=None) -> None:
        # broker is a ProjectXBroker; reuse its token + contract-id resolution.
        self._broker = broker
        self._mock = getattr(broker, "_mock_mode", True) or not _SIGNALR_AVAILABLE
        self._engines: dict[str, OrderFlowEngine] = {}   # root symbol -> engine
        self._cid_to_sym: dict[str, str] = {}            # contractId -> root symbol
        self._subscribed: set[str] = set()
        self._sub_cids: list[str] = []                   # contractIds to re-subscribe on reconnect
        self._conn = None
        self.depth_available = _SIGNALR_AVAILABLE and not getattr(broker, "_mock_mode", True)
        self._last_heal_ts = 0.0
        # Timestamp of the last successful on_reconnect callback. heal_if_stale
        # uses this to avoid tearing down and rebuilding a connection that just
        # reconnected — the re-subscribe may take a few seconds to start
        # delivering ticks, but that's not a sign the connection is dead.
        self._last_reconnect_ts = 0.0
        # Optional callable invoked (from signalrcore's own background thread —
        # must be thread-safe; threading.Event.set is) the first time a live
        # quote/trade tick is observed in a fresh wall-clock bar bucket, so the
        # engine's decision loop can wake immediately instead of waiting out a
        # full poll interval. None (the default) disables this entirely.
        self._on_boundary = on_boundary
        self._last_bucket: int | None = None

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
        """Subscribe quotes + trades + depth for every futures root.

        Non-futures symbols (no FUTURES_SPECS entry) are skipped — order flow is
        a futures-only signal here. Returns the count actually subscribed.
        """
        if self._mock:
            reason = ("signalrcore not installed (pip install signalrcore)"
                      if not _SIGNALR_AVAILABLE else "broker in mock mode")
            log.info(f"[OrderFlow] {reason} — no live subscription; gate fails open")
            return 0

        # Resolve futures roots → contractIds up front (skips unknowns).
        targets: list[tuple[str, str]] = []  # (root, contractId)
        for symbol in symbols:
            sym = symbol.upper()
            if spec_for(sym) is None or sym in self._subscribed:
                continue
            cid = self._broker.contract_id(sym)
            if not cid:
                log.warning(f"[OrderFlow] no contract for {sym}; skipping")
                continue
            self.get(sym)                 # ensure engine exists before ticks arrive
            self._cid_to_sym[cid] = sym
            targets.append((sym, cid))

        if not targets:
            log.info("[OrderFlow] no futures roots resolved — gate idle (fails open)")
            return 0

        if not self._connect():
            return 0

        n = 0
        for sym, cid in targets:
            try:
                self._conn.send("SubscribeContractQuotes", [cid])
                self._conn.send("SubscribeContractTrades", [cid])
                self._conn.send("SubscribeContractMarketDepth", [cid])
                self._subscribed.add(sym)
                if cid not in self._sub_cids:
                    self._sub_cids.append(cid)   # remember for reconnect re-subscribe
                n += 1
                log.info(f"[OrderFlow] subscribed {sym} ({cid}) "
                         "(GatewayQuote + GatewayTrade + GatewayDepth/L2)")
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[OrderFlow] subscribe failed for {sym}: {exc}")
        return n

    def _connect(self) -> bool:
        """Build + start the SignalR market-hub connection and register handlers."""
        if self._conn is not None:
            return True
        token = getattr(self._broker, "token", "")
        if not token:
            log.warning("[OrderFlow] broker has no auth token — cannot start feed")
            return False
        url = f"{CONFIG.projectx_rtc_base.rstrip('/')}/hubs/market?access_token={token}"
        try:
            self._conn = (
                HubConnectionBuilder()
                .with_url(url, options={"skip_negotiation": True})
                .with_automatic_reconnect({
                    "type": "raw", "keep_alive_interval": 5, "reconnect_interval": 2,
                })
                .build()
            )
            self._conn.on("GatewayQuote", self._on_quote)
            self._conn.on("GatewayTrade", self._on_trade)
            self._conn.on("GatewayDepth", self._on_depth)
            # Re-subscribe after an automatic reconnect: SignalR drops all server
            # subscriptions on a dropped connection, so without this the feed comes
            # back "connected" but silent → the book freezes → has_data goes stale
            # → the order-flow gate fails CLOSED (no blind entries on a dead feed).
            if hasattr(self._conn, "on_reconnect"):
                self._conn.on_reconnect(self._on_reconnect)
            else:
                # Without the hook an automatic reconnect comes back subscribed
                # to nothing — the feed looks connected but is silent. The
                # staleness self-heal (heal_if_stale) is the only recovery.
                log.warning("[OrderFlow] this signalrcore version has no "
                            "on_reconnect hook — relying on staleness self-heal "
                            "to recover dropped subscriptions")
            self._conn.start()
            log.info("[OrderFlow] SignalR market hub connected")
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[OrderFlow] market hub connect failed: {exc} — gate fails open")
            self._conn = None
            return False

    def _on_reconnect(self) -> None:
        """Replay every contract subscription after an automatic reconnect.

        Called by signalrcore after a successful automatic reconnect. The
        reconnect reuses the hub URL that was built in _connect(), which
        carries the access token at that point in time. If the token has
        since been refreshed, the reconnect URL carries the old token — the
        gateway may accept it (the connection is already established) but
        refresh defensively so the broker HTTP client and the hub are in sync.

        Also stamps _last_reconnect_ts so heal_if_stale doesn't treat the
        brief quote silence while re-subscribes start flowing as a dead feed
        and immediately tear down the just-reconnected connection.
        """
        self._last_reconnect_ts = time.time()
        if self._conn is None:
            return
        # Refresh the token before re-subscribing. The reconnect URL carries
        # the original access token — if it was rotated while the connection
        # was down this defensive refresh keeps the broker HTTP client (used
        # for all REST calls including order placement) current.
        try:
            self._broker._ensure_token()   # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[OrderFlow] token refresh on reconnect failed: {exc}")
        log.warning(f"[OrderFlow] SignalR reconnected — re-subscribing "
                    f"{len(self._sub_cids)} contract(s)")
        failed = []
        for cid in self._sub_cids:
            try:
                self._conn.send("SubscribeContractQuotes", [cid])
                self._conn.send("SubscribeContractTrades", [cid])
                self._conn.send("SubscribeContractMarketDepth", [cid])
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[OrderFlow] re-subscribe failed for {cid}: {exc}")
                failed.append(cid)
        if failed:
            log.error(
                f"[OrderFlow] {len(failed)}/{len(self._sub_cids)} contracts "
                f"failed to re-subscribe after reconnect — feed may be partially "
                f"dead. heal_if_stale will rebuild after {self._HEAL_AFTER_S}s of "
                f"silence: {failed}"
            )

    # ── SignalR event handlers (args = [contractId, payload]) ─────────────────

    def _maybe_signal_boundary(self) -> None:
        """Fire on_boundary the first time a tick lands in a fresh wall-clock
        bar bucket (same CONFIG.bar_align_sec grid the engine's poll cadence
        aligns to). Debounced to once per bucket; never raises — a signaling
        problem must never take down the tick handler that called it."""
        if self._on_boundary is None:
            return
        try:
            period = CONFIG.bar_align_sec
            if period <= 0:
                return
            bucket = int(time.time() // period)
            if bucket != self._last_bucket:
                self._last_bucket = bucket
                self._on_boundary()
        except Exception as exc:  # noqa: BLE001
            log.debug(f"[OrderFlow] boundary signal failed: {exc}")

    def _on_quote(self, args) -> None:
        try:
            self._maybe_signal_boundary()
            cid, data = args[0], args[1]
            eng = self._engine_for(cid)
            if eng is None or not isinstance(data, dict):
                return
            bid = float(data.get("bestBid", 0) or 0)
            ask = float(data.get("bestAsk", 0) or 0)
            # Sizes are not always present on the quote; default 0 (depth feed
            # supplies the real ladder sizes for OBI when available).
            bid_size = float(data.get("bestBidSize", data.get("bidSize", 0)) or 0)
            ask_size = float(data.get("bestAskSize", data.get("askSize", 0)) or 0)
            # Reject one-sided / zero / crossed books — only a sane two-sided market
            # (0 < bid < ask) yields a trustworthy mark. A crossed or half-empty
            # quote (auction, gap, bad tick) must NOT record a mark or refresh the
            # freshness timestamp, so the book reads stale instead of poisoned.
            if 0 < bid < ask:
                eng.on_depth(bid=bid, bid_size=bid_size, ask=ask, ask_size=ask_size)
        except Exception as exc:  # noqa: BLE001 - never let a bad tick kill the loop
            log.debug(f"[OrderFlow] quote handler error: {exc}")

    def _on_trade(self, args) -> None:
        try:
            self._maybe_signal_boundary()
            cid, data = args[0], args[1]
            eng = self._engine_for(cid)
            if eng is None:
                return
            # Trade payload may be a single dict or a list of prints.
            prints = data if isinstance(data, list) else [data]
            for t in prints:
                if not isinstance(t, dict):
                    continue
                price = float(t.get("price", 0) or 0)
                size = float(t.get("volume", t.get("size", 0)) or 0)
                if price > 0 and size > 0:
                    eng.on_trade(price, size)
        except Exception as exc:  # noqa: BLE001
            log.debug(f"[OrderFlow] trade handler error: {exc}")

    def _on_depth(self, args) -> None:
        try:
            cid, data = args[0], args[1]
            eng = self._engine_for(cid)
            if eng is None:
                return
            entries = data if isinstance(data, list) else [data]
            for e in entries:
                if not isinstance(e, dict):
                    continue
                dom = int(e.get("type", -1))
                price = float(e.get("price", 0) or 0)
                volume = float(e.get("volume", 0) or 0)
                if price <= 0:
                    continue
                if dom in _BID_TYPES:
                    eng.on_depth_update("bid", price, volume)
                elif dom in _ASK_TYPES:
                    eng.on_depth_update("ask", price, volume)
                # Trade/Reset/High/Low/Fill DomTypes carry no resting-book change.
        except Exception as exc:  # noqa: BLE001
            log.debug(f"[OrderFlow] depth handler error: {exc}")

    def _engine_for(self, contract_id) -> OrderFlowEngine | None:
        sym = self._cid_to_sym.get(str(contract_id))
        return self._engines.get(sym) if sym else None

    # Rebuild the hub after this much feed silence (engine gates entries at
    # 15s staleness already — this is the deeper "connection is dead" repair).
    _HEAL_AFTER_S = 60
    _HEAL_COOLDOWN_S = 60  # min spacing between rebuild attempts
    # Hard ceiling: once the feed has been silent this long, force a full rebuild
    # even if an automatic reconnect looks recent. Repeated socket-close/reconnect
    # churn keeps stamping _last_reconnect_ts, which would otherwise defer the
    # staleness heal indefinitely while no data actually flows (observed 2600s+
    # silences with the market open).
    _HEAL_HARD_S = 300

    def heal_if_stale(self) -> None:
        """Self-heal a silently-dead connection: if every engine has been
        quote-silent for _HEAL_AFTER_S, tear the hub down and rebuild it with a
        freshly-validated token, then re-subscribe everything.

        Covers the two failure modes automatic reconnect cannot: (a) a
        signalrcore build without the on_reconnect hook (reconnects subscribed
        to nothing), and (b) token expiry — the reconnect URL carries the
        ORIGINAL access token, which the gateway rejects after rotation.
        Callers must only invoke this while the market is open; outside trading
        hours silence is normal.

        Respects a post-reconnect quiet window (HEAL_COOLDOWN_S from the last
        on_reconnect callback) so a freshly-reconnected feed that has not yet
        delivered its first tick is not immediately torn down again."""
        if self._mock or self._conn is None or not self._subscribed:
            return
        if not self._engines:
            return  # guard against max() on empty sequence
        freshest = max((getattr(e, "last_quote_ts", 0.0) or 0.0)
                       for e in self._engines.values())
        now = time.time()
        if freshest <= 0 or now - freshest < self._HEAL_AFTER_S:
            return
        if now - self._last_heal_ts < self._HEAL_COOLDOWN_S:
            return
        # Give the automatic-reconnect path its own cooldown window: if
        # _on_reconnect fired recently, the re-subscriptions may not have
        # produced a quote yet even though the connection is healthy. Past the
        # hard-silence ceiling, stop deferring — the reconnect churn is not
        # delivering data and would otherwise defer the rebuild forever.
        if (now - freshest < self._HEAL_HARD_S
                and now - self._last_reconnect_ts < self._HEAL_COOLDOWN_S):
            log.debug(
                f"[OrderFlow] feed silent {now - freshest:.0f}s but reconnect was "
                f"{now - self._last_reconnect_ts:.0f}s ago — giving re-subscriptions "
                f"more time before declaring the connection dead"
            )
            return
        self._last_heal_ts = now
        log.warning(f"[OrderFlow] feed silent {now - freshest:.0f}s with market open — "
                    "rebuilding SignalR connection with a fresh token")
        try:
            self._broker._ensure_token()   # refresh/re-login before rebuilding the URL
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[OrderFlow] token refresh before heal failed: {exc}")
        roots = sorted(self._subscribed)
        self.close()
        self._subscribed.clear()
        self._sub_cids.clear()
        n = self.subscribe(roots)
        log.warning(f"[OrderFlow] self-heal re-subscribed {n}/{len(roots)} root(s)")
        from exec_telemetry import TELEM
        TELEM.record("feed_heal", silent_s=round(now - freshest, 1), resubscribed=n)

    def reset_session(self) -> None:
        """Zero every engine's CVD at the open."""
        for eng in self._engines.values():
            eng.reset_session()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.stop()
            except Exception:  # noqa: BLE001
                pass
            self._conn = None
