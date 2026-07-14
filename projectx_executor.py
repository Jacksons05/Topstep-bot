"""ProjectX (TopstepX) broker adapter for Topstep Trading integration.

Talks to the ProjectX Gateway REST API (https://api.topstepx.com). ProjectX is
Topstep's own gateway — simple API-key auth (no Rithmic app-registration /
conformance wall that blocked the old rithmic_executor with rp_code 13).

Architecture:
  ProjectXBroker mirrors RithmicBroker / AlpacaBroker's interface exactly so
  engine.py / executor.py need zero changes after the swap. All REST calls are
  synchronous (httpx.Client) — no background asyncio loop is needed (the old
  Rithmic adapter needed one only because async-rithmic was async). The live
  order-flow feed (projectx_marketdata.ProjectXOrderFlowFeed) reuses this
  broker's session token + contract-id resolution for its SignalR connection.

  When credentials are absent the broker falls back to MOCK MODE — every call
  logs and returns a safe placeholder so the engine boots and the risk layer can
  be exercised without a live connection.

Environment variables (set in .env):
  PROJECTX_USERNAME      — TopstepX username / login
  PROJECTX_API_KEY       — API key (TopstepX → Settings → API Keys)
  PROJECTX_API_BASE      — REST base (default https://api.topstepx.com)
  PROJECTX_ACCOUNT_NAME  — optional; blank picks the first tradable account
  PROJECTX_LIVE          — False = sim/eval, True = funded (controls Contract `live`)
  TOPSTEP_MODE_ENABLED     — must be True to activate this broker

REST endpoints used (all POST, JSON, JWT Bearer after login):
  /api/Auth/loginKey        {userName, apiKey}            -> {token, success, ...}
  /api/Auth/validate        (Bearer)                      -> {newToken, success, ...}
  /api/Account/search       {onlyActiveAccounts}          -> {accounts:[{id,name,balance,canTrade}]}
  /api/Contract/search      {searchText, live}            -> {contracts:[{id,name,activeContract}]}
  /api/Order/place          {accountId,contractId,type,side,size,customTag} -> {orderId, success}
  /api/Order/cancel         {accountId, orderId}          -> {success}
  /api/Position/searchOpen  {accountId}                   -> {positions:[{contractId,type,size,averagePrice}]}
  /api/Position/closeContract {accountId, contractId}     -> {success}

Enums: Order type 2 = Market. Side 0 = Bid(buy), 1 = Ask(sell).
       Position type 1 = long, 2 = short.
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from broker import Fill
from exec_telemetry import TELEM, signed_slippage
from config import CONFIG
from futures_symbols import spec_for

log = logging.getLogger(__name__)


class OrderStateUnknown(RuntimeError):
    """A non-idempotent request (order placement) failed AFTER it may have
    reached the gateway. The order may or may not exist — the caller must NOT
    resubmit; the ambiguity resolves through position reconciliation."""

# ── ProjectX enum constants ───────────────────────────────────────────────────
_ORDER_TYPE_LIMIT = 1
_ORDER_TYPE_MARKET = 2
_ORDER_TYPE_STOP = 4   # resting stop (stopPrice). VERIFY enum + field in live-sim.
_SIDE_BUY = 0   # Bid
_SIDE_SELL = 1  # Ask
_POS_LONG = 1   # Position.type long (else short)

# Session tokens are valid 24h; revalidate with a safety margin before expiry.
_TOKEN_TTL_SEC = 23 * 3600


class ProjectXBroker:
    """ProjectX/TopstepX order execution. Drop-in replacement for RithmicBroker.

    Activated when TOPSTEP_MODE_ENABLED=True and PROJECTX_USERNAME + PROJECTX_API_KEY
    are set. Falls back to mock mode if credentials are blank — safe to
    instantiate in either case.
    """

    name = "projectx"

    def __init__(self) -> None:
        self._user: str = CONFIG.projectx_username
        self._api_key: str = CONFIG.projectx_api_key
        self._base: str = CONFIG.projectx_api_base.rstrip("/")
        self._live: bool = CONFIG.projectx_live

        self.token: str = ""
        self._token_ts: float = 0.0
        self.account_id: int = 0
        self._account_name: str = ""

        # symbol root <-> ProjectX contractId caches. Resolution is re-checked
        # once per UTC day so a quarterly roll (activeContract flips to the next
        # month) is picked up instead of trading the expiring contract forever.
        self._root_to_cid: dict[str, str] = {}
        self._cid_to_root: dict[str, str] = {}
        self._cid_resolved_on: dict[str, "date"] = {}  # root -> date last resolved

        self._http: httpx.Client | None = None

        self._mock_mode: bool = not (self._user and self._api_key)
        if self._mock_mode:
            log.warning(
                "ProjectXBroker MOCK MODE (PROJECTX_USERNAME / PROJECTX_API_KEY not set). "
                "No real orders will be placed."
            )
            return

        # Shorter keepalive_expiry than a typical server idle-drop window forces
        # httpx to open fresh connections proactively instead of handing back a
        # pooled one the server may have already killed — the main source of the
        # "SSL: BAD_LENGTH" / "EOF violation" resets seen on this connection pool.
        self._http = httpx.Client(
            base_url=self._base, timeout=8.0,
            limits=httpx.Limits(max_keepalive_connections=5, keepalive_expiry=20.0),
        )
        try:
            self._authenticate()
            self._load_account()
            log.info(
                f"[ProjectX] connected | account={self._account_name} "
                f"(id={self.account_id}) | live={self._live}"
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                f"[ProjectX] connection failed: {exc}. Falling back to mock mode — "
                "check PROJECTX_USERNAME / PROJECTX_API_KEY."
            )
            self._mock_mode = True

    # ── HTTP / auth helpers ───────────────────────────────────────────────────

    def _authenticate(self) -> None:
        """Exchange username + API key for a JWT session token."""
        r = self._http.post(
            "/api/Auth/loginKey",
            json={"userName": self._user, "apiKey": self._api_key},
        )
        r.raise_for_status()
        d = r.json()
        if not d.get("success") or not d.get("token"):
            raise RuntimeError(f"loginKey failed: errorCode={d.get('errorCode')} {d.get('errorMessage')}")
        self.token = d["token"]
        self._token_ts = time.time()
        self._http.headers["Authorization"] = f"Bearer {self.token}"

    def _ensure_token(self) -> None:
        """Revalidate the session token before it expires (24h TTL)."""
        if not self.token or (time.time() - self._token_ts) < _TOKEN_TTL_SEC:
            return
        try:
            r = self._http.post("/api/Auth/validate")
            r.raise_for_status()
            d = r.json()
            new = d.get("newToken")
            if d.get("success") and new:
                self.token = new
                self._token_ts = time.time()
                self._http.headers["Authorization"] = f"Bearer {self.token}"
                return
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ProjectX] token validate failed ({exc}); re-logging in")
        self._authenticate()  # fall back to a fresh login

    _TRANSPORT_RETRIES = 3
    _TRANSPORT_RETRY_BACKOFF_S = 0.4  # doubles each attempt: 0.4, 0.8, 1.6

    def _post(self, path: str, body: dict, *, mutating: bool = False) -> dict:
        """POST helper: refreshes token, retries once on 401, returns parsed JSON.

        Also retries on a transport-level failure (dropped keep-alive connection,
        SSL reset, etc.) — these are transient and common on long-lived connection
        pools, especially right after startup while the network path settles.
        A short exponential backoff between attempts avoids hammering a link
        that's still down; a fresh connection from the pool clears it otherwise.

        mutating=True marks a non-idempotent request (order placement). For
        those, only CONNECT-phase failures are retried — the request never left
        this machine, so a re-POST cannot double anything. Any error after the
        request may have been sent (read timeout, reset mid-response) raises
        OrderStateUnknown instead of re-POSTing: the gateway may have processed
        the first copy, and a blind retry is how duplicate fills happen. The
        caller resolves the ambiguity through position reconciliation.

        A 401 response is safe to re-POST even when mutating: the gateway
        received and REJECTED the request, so nothing was placed."""
        self._ensure_token()
        r = None
        for attempt in range(self._TRANSPORT_RETRIES):
            try:
                r = self._http.post(path, json=body)
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Connect phase failed — request never sent, always safe to retry.
                if attempt == self._TRANSPORT_RETRIES - 1:
                    raise
                backoff = self._TRANSPORT_RETRY_BACKOFF_S * (2 ** attempt)
                log.warning(f"[ProjectX] connect error on POST {path} ({exc}); "
                            f"retry {attempt + 1}/{self._TRANSPORT_RETRIES - 1} in {backoff:.1f}s")
                time.sleep(backoff)
            except httpx.TransportError as exc:
                # Sent (or partially sent) but no clean response.
                if mutating:
                    raise OrderStateUnknown(
                        f"POST {path} may have reached the gateway ({exc.__class__.__name__}: "
                        f"{exc}) — NOT retried; reconcile against broker positions"
                    ) from exc
                if attempt == self._TRANSPORT_RETRIES - 1:
                    raise
                backoff = self._TRANSPORT_RETRY_BACKOFF_S * (2 ** attempt)
                log.warning(f"[ProjectX] transport error on POST {path} ({exc}); "
                            f"retry {attempt + 1}/{self._TRANSPORT_RETRIES - 1} in {backoff:.1f}s")
                time.sleep(backoff)
        if r.status_code == 401:
            self._authenticate()
            r = self._http.post(path, json=body)
        r.raise_for_status()
        return r.json()

    def _load_account(self) -> None:
        d = self._post("/api/Account/search", {"onlyActiveAccounts": True})
        accounts = d.get("accounts") or []
        if not accounts:
            raise RuntimeError("no active ProjectX accounts returned")
        want = CONFIG.projectx_account_name.strip()
        chosen = None
        if want:
            chosen = next((a for a in accounts if a.get("name") == want), None)
            if chosen is None:
                log.warning(f"[ProjectX] account '{want}' not found; using first tradable")
        if chosen is None:
            chosen = next((a for a in accounts if a.get("canTrade")), accounts[0])
        self.account_id = int(chosen["id"])
        self._account_name = chosen.get("name", str(self.account_id))

    # ── contract resolution ───────────────────────────────────────────────────

    def contract_id(self, symbol: str) -> str | None:
        """Resolve a futures root (e.g. "ES") to its active ProjectX contractId.

        Cached per root. Returns None in mock mode or when no active contract is
        found. The feed and order paths share this cache so a symbol is resolved
        only once per session.
        """
        sym = symbol.upper()
        today = datetime.now(timezone.utc).date()
        cached = self._root_to_cid.get(sym)
        if cached is not None and self._cid_resolved_on.get(sym) == today:
            return cached
        if self._mock_mode:
            return None
        try:
            d = self._post("/api/Contract/search", {"searchText": sym, "live": self._live})
            contracts = d.get("contracts") or []
            # Prefer the active (front-month) contract; fall back to the first hit.
            active = next((c for c in contracts if c.get("activeContract")), None)
            chosen = active or (contracts[0] if contracts else None)
            if not chosen:
                log.warning(f"[ProjectX] no contract found for {sym}")
                return cached  # stale beats nothing mid-session
            cid = str(chosen["id"])
            if cached is not None and cid != cached:
                # Quarterly roll: the active front month changed while we were
                # holding a cache entry (and possibly a POSITION in the old
                # month). New orders/marks use the new contract; any open
                # position in the expiring month must be rolled manually.
                log.error(f"[ProjectX] CONTRACT ROLL {sym}: active contract changed "
                          f"{cached} -> {cid} — check for open positions in the "
                          f"expiring month; they will NOT roll automatically")
            self._root_to_cid[sym] = cid
            self._cid_to_root[cid] = sym
            self._cid_resolved_on[sym] = today
            if cid != cached:
                log.info(f"[ProjectX] resolved {sym} -> {cid} ({chosen.get('description', '')})")
            return cid
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ProjectX] contract_id({sym}) failed: {exc}")
            return cached  # keep trading on yesterday's resolution rather than halt

    # Alpaca-style "5Min" / "1Hour" / "1Day" -> ProjectX History unit enum.
    # ProjectX units: 1=Second, 2=Minute, 3=Hour, 4=Day, 5=Week, 6=Month.
    _TIMEFRAME_UNIT = {"Min": 2, "Hour": 3, "Day": 4, "Week": 5, "Month": 6}

    @classmethod
    def _parse_timeframe(cls, timeframe: str) -> tuple[int, int]:
        for suffix, unit in cls._TIMEFRAME_UNIT.items():
            if timeframe.endswith(suffix):
                n = timeframe[: -len(suffix)]
                return unit, int(n) if n else 1
        return 2, 5  # default: 5-minute bars

    def historical_bars(self, symbol: str, timeframe: str = "5Min",
                         limit: int = 200, days_back: int = 5) -> dict:
        """Recent OHLCV bars for a futures root via ProjectX's own history feed.

        Alpaca's /v2/stocks endpoint has no futures data at all — MNQ/ES/etc.
        always return empty there. This is the real bar source for this bot's
        watchlist. Returns the canonical {"close":[...],...} shape, oldest->newest,
        same as MarketData.bars(); empty dict on any failure (fail-open, matching
        MarketData.bars()'s contract so callers don't need to special-case source).
        """
        if self._mock_mode:
            return {}
        cid = self.contract_id(symbol)
        if not cid:
            return {}
        unit, unit_number = self._parse_timeframe(timeframe)
        now = datetime.now(timezone.utc)
        try:
            d = self._post("/api/History/retrieveBars", {
                "contractId": cid,
                "live": self._live,
                "startTime": (now - timedelta(days=days_back)).isoformat(),
                "endTime": now.isoformat(),
                "unit": unit,
                "unitNumber": unit_number,
                "limit": limit,
                "includePartialBar": True,
            })
            bars = d.get("bars") or []
            if not bars:
                return {}
            bars = list(reversed(bars))  # API returns newest-first; we want oldest->newest
            return {
                "open": [float(b["o"]) for b in bars],
                "high": [float(b["h"]) for b in bars],
                "low": [float(b["l"]) for b in bars],
                "close": [float(b["c"]) for b in bars],
                "volume": [float(b["v"]) for b in bars],
                # Per-bar UTC ISO timestamp (e.g. "2026-07-10T20:55:00+00:00").
                # Kept so the engine can anchor intraday VWAP to the 09:30 ET RTH
                # open instead of a rolling multi-session window. Alpaca's bars()
                # omits this key, so consumers must treat "time" as optional.
                "time": [str(b["t"]) for b in bars],
            }
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ProjectX] historical_bars({symbol}) failed: {exc}")
            return {}

    def root_for_contract(self, contract_id: str) -> str:
        """Reverse a contractId to its root symbol (best-effort, for positions)."""
        if contract_id in self._cid_to_root:
            return self._cid_to_root[contract_id]
        # contractId looks like "CON.F.US.EP.M25" — pull the product token (4th field)
        # and map known aliases back to our roots; else return the raw token.
        parts = contract_id.split(".")
        token = parts[3] if len(parts) >= 4 else contract_id
        alias = {"EP": "ES", "ENQ": "NQ", "MES": "MES", "MNQ": "MNQ"}
        return alias.get(token, token)

    # ── session lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        if self._http is not None:
            try:
                self._http.close()
                log.info("[ProjectX] session closed")
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[ProjectX] close error: {exc}")

    # ── core broker interface (mirrors RithmicBroker exactly) ────────────────

    def submit(self, symbol: str, qty: float, side: str, ref_price: float) -> Fill:
        """Place a market order for a futures contract."""
        # Kill-switch choke point: re-check at the actual order-send site, not
        # just once per cycle in the engine. Arming the switch mid-cycle (file
        # created / KILL_SWITCH=1) must stop an order already past the engine's
        # per-cycle check. Function-local import avoids a module-load cycle.
        from risk import kill_switch_active
        if kill_switch_active():
            log.error(f"[ProjectX] kill-switch ARMED — refusing order: "
                      f"{side} {qty}x {symbol}")
            raise RuntimeError(f"kill-switch active: order refused ({symbol})")
        n_contracts = max(1, int(qty))
        log.info(
            f"[ProjectX] {'MOCK' if self._mock_mode else 'LIVE'} submit | "
            f"{side} {n_contracts}x {symbol} @ ~{ref_price:.2f}"
        )
        if self._mock_mode:
            return Fill(
                symbol=symbol, qty=float(n_contracts), side=side, price=ref_price,
                order_id=f"projectx-mock-{uuid.uuid4().hex[:8]}", status="filled",
            )
        # Choke-point guard (real orders only): this bot must never order
        # outside its watchlist. Unattributed full-size ES orders kept
        # appearing on the account with our customTag; if any code path in
        # THIS process tries that, refuse and dump the stack so the culprit
        # is identified.
        watch = {w.upper() for w in CONFIG.watchlist}
        if symbol.upper() not in watch:
            import traceback
            log.error(f"[ProjectX] BLOCKED off-watchlist order: {side} {qty}x "
                      f"{symbol} — call stack:\n" + "".join(traceback.format_stack()))
            raise RuntimeError(f"off-watchlist order blocked: {symbol}")

        cid = self.contract_id(symbol)
        if cid is None:
            raise RuntimeError(f"[ProjectX] cannot resolve contract for {symbol}")

        body = {
            "accountId": self.account_id,
            "contractId": cid,
            "type": _ORDER_TYPE_MARKET,
            "side": _SIDE_BUY if side == "BUY" else _SIDE_SELL,
            "size": n_contracts,
            "customTag": f"jarvis_{symbol}_{uuid.uuid4().hex[:8]}",
        }
        t0 = time.monotonic()
        try:
            d = self._post("/api/Order/place", body, mutating=True)
        except OrderStateUnknown:
            TELEM.record("order_ambiguous", symbol=symbol, side=side, qty=n_contracts)
            raise
        latency_ms = round((time.monotonic() - t0) * 1000, 1)
        if not d.get("success"):
            TELEM.record("order_rejected", symbol=symbol, side=side, qty=n_contracts,
                         error_code=d.get("errorCode"), error_message=d.get("errorMessage"))
            raise RuntimeError(
                f"[ProjectX] order rejected: errorCode={d.get('errorCode')} {d.get('errorMessage')}"
            )
        order_id = str(d.get("orderId", ""))
        TELEM.record("order_submitted", symbol=symbol, side=side, qty=n_contracts,
                     ref_price=ref_price, latency_ms=latency_ms)
        # Market orders fill near-instantly. The place response carries no avg
        # fill price, so poll /api/Trade/search briefly for the REAL fill —
        # booking at ref_price hides slippage and drifts the internal ledger
        # away from broker truth (the false-MLL-breach fuel). Status stays
        # "filled" either way: with "accepted" the Position is created
        # filled=False and exit management never runs.
        price = ref_price
        confirm_ms = None
        t1 = time.monotonic()
        for _ in range(3):
            time.sleep(0.4)
            avg = self._avg_fill_price(order_id)
            if avg is not None:
                confirm_ms = round((time.monotonic() - t1) * 1000, 1)
                if abs(avg - ref_price) > 1e-9:
                    log.info(f"[ProjectX] {symbol} actual fill {avg:.4f} vs ref "
                             f"{ref_price:.4f} (slippage booked)")
                price = avg
                break
        spec = spec_for(symbol)
        slip_ticks, slip_usd = (None, None)
        if spec and confirm_ms is not None:
            slip_ticks, slip_usd = signed_slippage(side, ref_price, price,
                                                   spec.tick_size, spec.tick_value)
            slip_usd = round(slip_usd * n_contracts, 2)
        TELEM.record("order_filled", symbol=symbol, side=side, qty=n_contracts,
                     ref_price=ref_price, fill_price=price,
                     slippage_ticks=slip_ticks, slippage_usd=slip_usd,
                     confirm_ms=confirm_ms)
        return Fill(
            symbol=symbol, qty=float(n_contracts), side=side, price=price,
            order_id=order_id, status="filled",
        )

    def _avg_fill_price(self, order_id: str) -> float | None:
        """Size-weighted average fill price for an order via /api/Trade/search
        (schema verified 2026-07-03: trades[{orderId, price, size, voided}]).
        Returns None when no (non-voided) fills are visible yet or on error."""
        if not order_id:
            return None
        try:
            start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            d = self._post("/api/Trade/search",
                           {"accountId": self.account_id, "startTimestamp": start})
            fills = [t for t in d.get("trades", [])
                     if str(t.get("orderId", "")) == str(order_id)
                     and not t.get("voided")]
            total = sum(float(t.get("size", 0)) for t in fills)
            if total <= 0:
                return None
            return sum(float(t["price"]) * float(t.get("size", 0))
                       for t in fills) / total
        except Exception as exc:  # noqa: BLE001
            log.debug(f"[ProjectX] trade search for order {order_id} failed: {exc}")
            return None

    def place_stop_order(self, symbol: str, qty: int, side: str,
                         stop_price: float) -> str:
        """Place a NATIVE exchange-resting protective STOP order (C1).

        Submitted on the OPPOSITE side of the entry immediately after the fill so
        the position is protected on the exchange even if the bot crashes or the
        feed drops — the client-side polled stop is no longer the only line of
        defense against the trailing MLL.

        Returns the ProjectX orderId (so the caller can cancel it on a managed
        exit), or "" on failure / mock mode.

        VERIFIED 2026-07-03 against the sim gateway (account 7904116): type=4 +
        `stopPrice` accepted, orderId returned, cancel round-trip OK. A true
        OCO/bracket that auto-cancels the sibling on fill is NOT used here —
        the entry's take-profit stays client-side, and the engine cancels this
        resting stop when it flattens.
        """
        n = max(1, int(qty))
        if self._mock_mode:
            log.info(f"[ProjectX] MOCK protective STOP | {side} {n}x {symbol} @ {stop_price:.2f}")
            return f"projectx-mock-stop-{uuid.uuid4().hex[:8]}"
        cid = self.contract_id(symbol)
        if cid is None:
            log.error(f"[ProjectX] protective STOP: cannot resolve contract for {symbol}")
            return ""
        # Snap the stop to the contract tick grid — an off-tick stopPrice is
        # rejected by the exchange. Round CONSERVATIVELY (toward worse fill) so the
        # rounded stop is never further from entry than the risk-sized stop: a SELL
        # stop (protecting a long) rounds UP, a BUY stop (protecting a short) DOWN.
        stop_px = float(stop_price)
        spec = spec_for(symbol)
        if spec and spec.tick_size > 0:
            ticks = stop_px / spec.tick_size
            ticks = math.ceil(ticks) if side == "SELL" else math.floor(ticks)
            stop_px = ticks * spec.tick_size
        body = {
            "accountId": self.account_id,
            "contractId": cid,
            "type": _ORDER_TYPE_STOP,
            "side": _SIDE_BUY if side == "BUY" else _SIDE_SELL,
            "size": n,
            "stopPrice": round(stop_px, 6),
            "customTag": f"jarvis_stop_{symbol}_{uuid.uuid4().hex[:8]}",
        }
        try:
            d = self._post("/api/Order/place", body, mutating=True)
        except OrderStateUnknown as exc:
            # The stop MAY be resting untracked at the exchange. Surface loudly:
            # an untracked stop that later fills opens an unmanaged position.
            log.error(f"[ProjectX] protective STOP for {symbol} in UNKNOWN state "
                      f"({exc}) — check working orders on the account and cancel "
                      f"any duplicate jarvis_stop_{symbol}_* order manually")
            TELEM.record("stop_reject", symbol=symbol, reason="ambiguous")
            return ""
        except Exception as exc:  # noqa: BLE001
            log.error(f"[ProjectX] protective STOP submit failed for {symbol}: {exc}")
            TELEM.record("stop_reject", symbol=symbol, reason=str(exc))
            return ""
        if not d.get("success"):
            log.error(f"[ProjectX] protective STOP rejected: errorCode="
                      f"{d.get('errorCode')} {d.get('errorMessage')}")
            TELEM.record("stop_reject", symbol=symbol,
                         reason=f"errorCode={d.get('errorCode')}")
            return ""
        oid = str(d.get("orderId", ""))
        log.info(f"[ProjectX] protective STOP resting | {side} {n}x {symbol} "
                 f"@ {stop_price:.2f} (order {oid})")
        TELEM.record("stop_placed", symbol=symbol, side=side, qty=n,
                     stop_price=stop_px, order_id=oid)
        return oid

    def get_fill(self, order_id: str) -> tuple[str, float | None]:
        """Poll fill status. Market orders fill near-instantly; report the real
        size-weighted average price from /api/Trade/search when visible, else
        filled-at-provisional (None) so callers keep their ref price."""
        if self._mock_mode:
            return "filled", None
        return "filled", self._avg_fill_price(order_id)

    def account(self) -> dict:
        """Fetch account balance/equity from ProjectX."""
        if self._mock_mode:
            return {
                "cash": CONFIG.bankroll_usd, "equity": CONFIG.bankroll_usd,
                "buying_power": CONFIG.bankroll_usd, "source": "projectx-mock",
            }
        try:
            d = self._post("/api/Account/search", {"onlyActiveAccounts": True})
            accounts = d.get("accounts") or []
            acct = next((a for a in accounts if int(a.get("id", 0)) == self.account_id), None)
            if acct is None and accounts:
                acct = accounts[0]
            bal = float(acct.get("balance", CONFIG.bankroll_usd)) if acct else CONFIG.bankroll_usd
            return {
                "cash": bal, "equity": bal, "buying_power": bal,
                "source": "projectx", "account_id": self.account_id,
            }
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ProjectX] account() failed: {exc}")
            return {
                "cash": CONFIG.bankroll_usd, "equity": CONFIG.bankroll_usd,
                "buying_power": CONFIG.bankroll_usd, "source": "projectx-error",
            }

    def submit_option(self, structure, qty: float, ref_price: float,
                      opening: bool = True) -> Fill:
        """Options are NOT supported in Topstep/futures mode."""
        raise NotImplementedError(
            "ProjectXBroker does not support options. "
            "TRADE_OPTIONS is auto-disabled when TOPSTEP_MODE_ENABLED=True."
        )

    # ── extended / Topstep-specific methods ────────────────────────────────────

    def place_order(self, symbol: str, qty: int, side: str, price: float | None = None,
                    order_type: str = "market", time_in_force: str = "day") -> Fill:
        """Place a market order (convenience alias wrapping submit)."""
        return self.submit(symbol, float(qty), side, price or 0.0)

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by its ProjectX orderId."""
        log.info(f"[ProjectX] cancel_order({order_id}) | mock={self._mock_mode}")
        if self._mock_mode:
            return True
        try:
            d = self._post("/api/Order/cancel", {
                "accountId": self.account_id, "orderId": int(order_id),
            })
            ok = bool(d.get("success"))
            TELEM.record("order_cancelled", order_id=order_id, ok=ok)
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ProjectX] cancel_order({order_id}) failed: {exc}")
            TELEM.record("order_cancelled", order_id=order_id, ok=False)
            return False

    def get_positions(self) -> list[dict]:
        """Fetch open futures positions from ProjectX."""
        # debug: called every reconcile (~5s) — INFO here floods the log
        log.debug(f"[ProjectX] get_positions() | mock={self._mock_mode}")
        if self._mock_mode:
            return []
        try:
            d = self._post("/api/Position/searchOpen", {"accountId": self.account_id})
            positions = []
            for p in d.get("positions") or []:
                cid = str(p.get("contractId", ""))
                size = float(p.get("size", 0) or 0)
                if not cid or size == 0:
                    continue
                positions.append({
                    "symbol": self.root_for_contract(cid),
                    "contract_id": cid,
                    "qty": abs(size),
                    "side": "BUY" if int(p.get("type", 0)) == _POS_LONG else "SELL",
                    "avg_price": float(p.get("averagePrice", 0) or 0),
                    "unrealized_pnl": 0.0,  # not returned by searchOpen
                })
            return positions
        except Exception as exc:  # noqa: BLE001
            # Re-raise: returning [] here is indistinguishable from "flat at
            # broker" and makes the reconciler phantom-close the entire local
            # book (and flatten_all() a no-op) on a transient API error.
            log.warning(f"[ProjectX] get_positions() failed: {exc}")
            raise

    def get_account(self) -> dict:
        """Alias for account() — kept for compatibility."""
        return self.account()

    def flatten_all(self) -> dict[str, bool]:
        """Close ALL open positions (EOD/breach Topstep flatten) via closeContract.

        Returns a {contract_id: closed_ok} map so the caller can confirm which
        positions the broker actually flattened and retry the rest, rather than
        assuming success. RAISES on a get_positions() outage (unknown broker
        state) so the caller does NOT treat an API error as 'flat' and
        phantom-close the local book — a failed flatten must be retried, not
        silently dropped."""
        log.info(f"[ProjectX] flatten_all() | mock={self._mock_mode}")
        if self._mock_mode:
            log.info("[ProjectX] MOCK flatten_all — no real orders placed")
            return {}
        result: dict[str, bool] = {}
        # get_positions() may raise on a transient API error; let it propagate
        # so the caller retries next scan instead of booking a phantom flat.
        for pos in self.get_positions():
            cid = pos.get("contract_id")
            if not cid:
                continue
            try:
                d = self._post("/api/Position/closeContract", {
                    "accountId": self.account_id, "contractId": cid,
                })
                ok = bool(d.get("success"))
                result[cid] = ok
                log.info(f"[ProjectX] flatten {pos['symbol']} ({cid}): "
                         f"{'closed' if ok else 'FAILED ' + str(d.get('errorMessage'))}")
            except Exception as exc:  # noqa: BLE001
                result[cid] = False
                log.error(f"[ProjectX] closeContract({cid}) failed: {exc}")
        return result
