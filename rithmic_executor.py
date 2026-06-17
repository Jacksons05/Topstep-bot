"""Rithmic broker adapter for Lucid Trading integration.

Uses the `async-rithmic` package (pip install async-rithmic) which wraps
Rithmic's WebSocket + protobuf protocol behind a clean async Python API.

Architecture:
  RithmicBroker mirrors AlpacaBroker's interface exactly so engine.py /
  executor.py need zero changes after the swap. An internal asyncio event loop
  runs in a background daemon thread; all sync methods delegate via
  asyncio.run_coroutine_threadsafe() → future.result().

  When credentials are absent or async-rithmic is not installed, the broker
  falls back to MOCK MODE — every call logs and returns a safe placeholder so
  the engine boots and the risk layer can be exercised without a live connection.

Environment variables (set in .env):
  RITHMIC_USER      — your Rithmic username
  RITHMIC_PASSWORD  — your Rithmic password
  RITHMIC_SYSTEM    — gateway system name (e.g. "Rithmic Paper Trading" or "Rithmic 01")
  RITHMIC_ENV       — "paper" | "live"
  RITHMIC_URL       — WebSocket URL (test: rituz00100.rithmic.com:443;
                      production URL is provided by Rithmic after conformance)
  LUCID_MODE_ENABLED — must be True to activate this broker

Protobuf field reference (verified from async-rithmic 1.6.2 inspection):
  AccountPnLPositionUpdate (template 451):
    account_balance, cash_on_hand, available_buying_power,
    open_position_pnl, day_pnl

  InstrumentPnLPositionUpdate (template 450, via list_positions):
    symbol, exchange, net_quantity, avg_open_fill_price, open_position_pnl

  ExchangeOrderNotification (template 352):
    user_tag      → the order_id string passed to submit_order
    notify_type   → ExchangeOrderNotificationType enum value (5 = FILL)
    avg_fill_price, total_fill_size, basket_id, account_id

NOTE on production URLs:
  Rithmic requires a one-time conformance test before issuing production gateway
  URLs. Contact Rithmic, run the conformance.py script from the async-rithmic
  repo, and they will provide your production URL. The test gateway
  (rituz00100.rithmic.com:443) works for development.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime
from typing import Any

from broker import Fill
from config import CONFIG
from futures_symbols import spec_for

log = logging.getLogger(__name__)

# ── Optional import: async-rithmic ────────────────────────────────────────────
try:
    from async_rithmic import (
        RithmicClient,
        OrderType,
        TransactionType,
        ExchangeOrderNotificationType,
    )
    _RITHMIC_AVAILABLE = True
    log.info("async-rithmic found — RithmicBroker ready for real wiring")
except ImportError:
    _RITHMIC_AVAILABLE = False
    log.warning(
        "async-rithmic not installed. RithmicBroker running in MOCK MODE. "
        "Install: pip install async-rithmic"
    )


class RithmicBroker:
    """Rithmic order execution broker. Drop-in replacement for AlpacaBroker.

    Activated when LUCID_MODE_ENABLED=True and RITHMIC_USER + RITHMIC_PASSWORD
    are set. Falls back to mock mode if the library is missing or credentials
    are blank — safe to instantiate in either case.
    """

    name = "rithmic"

    def __init__(self) -> None:
        self._user: str = CONFIG.rithmic_user
        self._password: str = CONFIG.rithmic_password
        self._system: str = CONFIG.rithmic_system
        self._env: str = CONFIG.rithmic_env
        self._url: str = CONFIG.rithmic_url

        self._connected: bool = False
        self._account_id: str = ""
        self._client: Any = None

        # Fill tracking: order_id → (threading.Event, fill_price)
        # Rithmic returns order_id as `user_tag` in ExchangeOrderNotification
        self._fill_events: dict[str, threading.Event] = {}
        self._fill_prices: dict[str, float | None] = {}
        self._fill_lock = threading.Lock()

        self._mock_mode: bool = (
            not _RITHMIC_AVAILABLE or not (self._user and self._password)
        )

        if self._mock_mode:
            reason = (
                "async-rithmic not installed" if not _RITHMIC_AVAILABLE
                else "credentials not set (RITHMIC_USER / RITHMIC_PASSWORD)"
            )
            log.warning(
                f"RithmicBroker MOCK MODE ({reason}). "
                "No real orders will be placed."
            )
            return

        # Persistent asyncio event loop running in a background daemon thread.
        # All async API calls are submitted here via run_coroutine_threadsafe().
        self._loop = asyncio.new_event_loop()
        self._bg_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="rithmic-event-loop",
        )
        self._bg_thread.start()
        self._connect()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _run(self, coro, timeout: float = 30.0):
        """Submit coroutine to background loop and block until result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def _connect(self) -> None:
        """Synchronous wrapper around the async connect sequence."""
        try:
            self._run(self._async_connect(), timeout=60)
        except Exception as exc:  # noqa: BLE001
            log.error(
                f"[Rithmic] Connection failed: {exc}. "
                "Falling back to mock mode — check credentials and RITHMIC_URL."
            )
            self._mock_mode = True

    async def _async_connect(self) -> None:
        """Establish WebSocket session, register fill callback, fetch account ID.

        RithmicClient delegates all plant methods directly onto itself:
          client.submit_order(...)               → OrderPlant.submit_order
          client.list_accounts()                 → OrderPlant.list_accounts
          client.list_account_summary()          → PnlPlant.list_account_summary
          client.list_positions()                → PnlPlant.list_positions
          client.get_front_month_contract(s, e)  → TickerPlant
          client.exit_position()                 → OrderPlant.exit_position
          client.cancel_order(order_id=..., ...) → OrderPlant.cancel_order
        """
        self._client = RithmicClient(
            user=self._user,
            password=self._password,
            system_name=self._system,
            app_name="JARVIS",
            app_version="1.0",
            url=self._url,  # wss:// prefix added automatically if absent
        )

        # Register fill callback BEFORE connect so we never miss a fill.
        # ExchangeOrderNotification (template 352) carries:
        #   user_tag       → the order_id string we passed to submit_order
        #   notify_type    → ExchangeOrderNotificationType (5 = FILL)
        #   avg_fill_price → actual execution price
        async def _on_exchange_order_notification(notification) -> None:
            ntype = getattr(notification, "notify_type", None)
            if ntype == ExchangeOrderNotificationType.FILL:
                oid = getattr(notification, "user_tag", None)
                if oid:
                    price = float(getattr(notification, "avg_fill_price", 0) or 0)
                    with self._fill_lock:
                        self._fill_prices[oid] = price
                        evt = self._fill_events.get(oid)
                    if evt is not None:
                        evt.set()
                        log.debug(f"[Rithmic] Fill: {oid} @ {price}")

        self._client.on_exchange_order_notification += _on_exchange_order_notification

        await self._client.connect()

        # Fetch the first account associated with this login.
        accounts = await self._client.list_accounts()
        self._account_id = accounts[0].account_id if accounts else ""
        self._connected = True
        log.info(
            f"[Rithmic] Connected | system={self._system} | "
            f"env={self._env} | account={self._account_id}"
        )

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Gracefully disconnect the Rithmic session and stop background loop."""
        if self._client is not None:
            try:
                self._run(self._client.disconnect(), timeout=10)
                log.info("[Rithmic] session closed")
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[Rithmic] close error: {exc}")
        self._connected = False
        if hasattr(self, "_loop") and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Core broker interface (mirrors AlpacaBroker exactly) ─────────────────

    def submit(self, symbol: str, qty: float, side: str, ref_price: float) -> Fill:
        """Place a market order for a futures contract.

        Args:
            symbol    : futures root (e.g. "ES", "MES", "NQ")
            qty       : number of contracts (fractional truncated to int)
            side      : "BUY" or "SELL"
            ref_price : reference price for P&L tracking / mock fill price
        """
        n_contracts = max(1, int(qty))
        spec = spec_for(symbol)
        exchange = spec.exchange if spec else "CME"

        log.info(
            f"[Rithmic] {'MOCK' if self._mock_mode else 'LIVE'} submit | "
            f"{side} {n_contracts}x {symbol} @ ~{ref_price:.2f} | "
            f"system={self._system} env={self._env}"
        )

        if self._mock_mode:
            return Fill(
                symbol=symbol,
                qty=float(n_contracts),
                side=side,
                price=ref_price,
                order_id=f"rithmic-mock-{uuid.uuid4().hex[:8]}",
                status="filled",
            )

        return self._run(
            self._async_submit(symbol, exchange, n_contracts, side, ref_price),
            timeout=30,
        )

    async def _async_submit(
        self,
        symbol: str,
        exchange: str,
        qty: int,
        side: str,
        ref_price: float,
    ) -> Fill:
        # Resolve root symbol to front-month contract code (e.g. ES → ESM6)
        try:
            security_code = await self._client.get_front_month_contract(symbol, exchange)
        except Exception:  # noqa: BLE001
            security_code = symbol  # fall back to root if resolution fails
            log.warning(f"[Rithmic] front-month resolution failed for {symbol}, using root")

        # order_id becomes user_tag in the protobuf; echoed back in fill notifications
        order_id = (
            f"jarvis_{symbol}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_{uuid.uuid4().hex[:6]}"
        )

        # Register fill waiter BEFORE submit — callback may fire synchronously
        evt = threading.Event()
        with self._fill_lock:
            self._fill_events[order_id] = evt
            self._fill_prices[order_id] = None

        await self._client.submit_order(
            order_id,           # positional: becomes user_tag in protobuf
            security_code,
            exchange,
            qty=qty,
            order_type=OrderType.MARKET,
            transaction_type=(
                TransactionType.BUY if side == "BUY" else TransactionType.SELL
            ),
            account_id=self._account_id or None,
        )

        # Market futures orders fill within milliseconds; wait up to 15 s
        filled = evt.wait(timeout=15)
        with self._fill_lock:
            fill_price = self._fill_prices.pop(order_id, ref_price) or ref_price
            self._fill_events.pop(order_id, None)

        return Fill(
            symbol=symbol,
            qty=float(qty),
            side=side,
            price=fill_price,
            order_id=order_id,
            status="filled" if filled else "accepted",
        )

    def get_fill(self, order_id: str) -> tuple[str, float | None]:
        """Poll fill status for an existing order.

        For Rithmic market orders the fill typically arrives inside submit().
        This handles the edge case where a fill is still in-flight.
        """
        if self._mock_mode:
            return "filled", None

        with self._fill_lock:
            evt = self._fill_events.get(order_id)
            if evt is None:
                price = self._fill_prices.pop(order_id, None)
                return "filled", price
            if evt.is_set():
                price = self._fill_prices.pop(order_id, None)
                self._fill_events.pop(order_id, None)
                return "filled", price

        return "accepted", None

    def account(self) -> dict:
        """Fetch account balance and equity from Rithmic.

        Protobuf fields (AccountPnLPositionUpdate / template 451):
          account_balance      — total account balance
          cash_on_hand         — available cash
          available_buying_power — buying power
          day_pnl, open_position_pnl
        """
        if self._mock_mode:
            return {
                "cash": CONFIG.bankroll_usd,
                "equity": CONFIG.bankroll_usd,
                "buying_power": CONFIG.bankroll_usd,
                "source": "rithmic-mock",
            }
        return self._run(self._async_account())

    async def _async_account(self) -> dict:
        try:
            summaries = await self._client.list_account_summary()
            if summaries:
                s = summaries[0]
                # Field names confirmed via protobuf schema (template 451)
                equity = float(getattr(s, "account_balance", None) or CONFIG.bankroll_usd)
                cash = float(getattr(s, "cash_on_hand", None) or equity)
                buying_power = float(getattr(s, "available_buying_power", None) or cash)
                return {
                    "cash": cash,
                    "equity": equity,
                    "buying_power": buying_power,
                    "day_pnl": float(getattr(s, "day_pnl", 0) or 0),
                    "open_pnl": float(getattr(s, "open_position_pnl", 0) or 0),
                    "source": "rithmic",
                    "account_id": self._account_id,
                }
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[Rithmic] account() failed: {exc}")
        return {
            "cash": CONFIG.bankroll_usd,
            "equity": CONFIG.bankroll_usd,
            "buying_power": CONFIG.bankroll_usd,
            "source": "rithmic-error",
        }

    def submit_option(self, structure, qty: float, ref_price: float,
                      opening: bool = True) -> Fill:
        """Options are NOT supported in Rithmic/Lucid mode.

        The engine auto-disables TRADE_OPTIONS when Lucid mode is active,
        so this path should never be reached in normal operation.
        """
        raise NotImplementedError(
            "RithmicBroker does not support options. "
            "TRADE_OPTIONS is auto-disabled when LUCID_MODE_ENABLED=True."
        )

    # ── Extended / Lucid-specific methods ────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        price: float | None = None,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> Fill:
        """Place a market order (convenience alias wrapping submit).

        Limit/stop orders are a future enhancement — currently all orders are
        submitted as market orders via submit().
        """
        return self.submit(symbol, float(qty), side, price or 0.0)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by user-assigned order_id (= user_tag).

        cancel_order() internally calls get_order(order_id=...) to look up
        the Rithmic basket_id, then submits the cancel request.
        """
        log.info(f"[Rithmic] cancel_order({order_id}) | mock={self._mock_mode}")
        if self._mock_mode:
            return True
        try:
            self._run(
                self._client.cancel_order(
                    order_id=order_id,
                    account_id=self._account_id or None,
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[Rithmic] cancel_order({order_id}) failed: {exc}")
            return False

    def get_positions(self) -> list[dict]:
        """Fetch open futures positions from Rithmic.

        Protobuf fields (InstrumentPnLPositionUpdate / template 450):
          symbol, exchange, net_quantity (+ = long, - = short),
          avg_open_fill_price, open_position_pnl

        Returns [] in mock mode.
        """
        log.info(f"[Rithmic] get_positions() | mock={self._mock_mode}")
        if self._mock_mode:
            return []
        return self._run(self._async_get_positions())

    async def _async_get_positions(self) -> list[dict]:
        try:
            # list_positions → template 402 → collect template 450 responses
            pos_msgs = await self._client.list_positions()
            positions = []
            for p in pos_msgs or []:
                sym = getattr(p, "symbol", None)
                net_qty = float(getattr(p, "net_quantity", 0) or 0)
                if not sym or net_qty == 0:
                    continue
                positions.append({
                    "symbol": sym,
                    "exchange": getattr(p, "exchange", ""),
                    "qty": abs(net_qty),
                    "side": "BUY" if net_qty > 0 else "SELL",
                    "avg_price": float(getattr(p, "avg_open_fill_price", 0) or 0),
                    "unrealized_pnl": float(getattr(p, "open_position_pnl", 0) or 0),
                })
            return positions
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[Rithmic] get_positions() failed: {exc}")
            return []

    def get_account(self) -> dict:
        """Alias for account() — kept for compatibility."""
        return self.account()

    def flatten_all(self) -> None:
        """Submit closing orders for ALL open positions (EOD Lucid flatten rule).

        Uses exit_position() — a single atomic close-all request.
        Falls back to iterating positions individually if that fails.
        """
        log.info(f"[Rithmic] flatten_all() | mock={self._mock_mode}")
        if self._mock_mode:
            log.info("[Rithmic] MOCK flatten_all — no real orders placed")
            return
        try:
            # exit_position() with no symbol/exchange closes ALL positions
            self._run(self._client.exit_position(), timeout=30)
            log.info("[Rithmic] All positions flattened via exit_position()")
        except Exception as exc:  # noqa: BLE001
            log.error(f"[Rithmic] exit_position() failed: {exc}. Trying fallback.")
            self._flatten_individually()

    def _flatten_individually(self) -> None:
        """Fallback: close each position with a separate market order."""
        try:
            positions = self.get_positions()
            for pos in positions:
                closing_side = "SELL" if pos["side"] == "BUY" else "BUY"
                self.submit(
                    pos["symbol"],
                    pos["qty"],
                    closing_side,
                    ref_price=pos.get("avg_price", 0.0),
                )
                log.info(
                    f"[Rithmic] flatten fallback: closed {pos['symbol']} "
                    f"{closing_side} {pos['qty']}"
                )
        except Exception as exc:  # noqa: BLE001
            log.error(f"[Rithmic] flatten_individually also failed: {exc}")
