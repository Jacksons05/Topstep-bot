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
import time
import uuid
from typing import Any

import httpx

from broker import Fill
from config import CONFIG
from futures_symbols import spec_for

log = logging.getLogger(__name__)

# ── ProjectX enum constants ───────────────────────────────────────────────────
_ORDER_TYPE_MARKET = 2
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

        # symbol root <-> ProjectX contractId caches
        self._root_to_cid: dict[str, str] = {}
        self._cid_to_root: dict[str, str] = {}

        self._http: httpx.Client | None = None

        self._mock_mode: bool = not (self._user and self._api_key)
        if self._mock_mode:
            log.warning(
                "ProjectXBroker MOCK MODE (PROJECTX_USERNAME / PROJECTX_API_KEY not set). "
                "No real orders will be placed."
            )
            return

        self._http = httpx.Client(base_url=self._base, timeout=15.0)
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

    def _post(self, path: str, body: dict) -> dict:
        """POST helper: refreshes token, retries once on 401, returns parsed JSON."""
        self._ensure_token()
        r = self._http.post(path, json=body)
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
        if sym in self._root_to_cid:
            return self._root_to_cid[sym]
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
                return None
            cid = str(chosen["id"])
            self._root_to_cid[sym] = cid
            self._cid_to_root[cid] = sym
            log.info(f"[ProjectX] resolved {sym} -> {cid} ({chosen.get('description', '')})")
            return cid
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ProjectX] contract_id({sym}) failed: {exc}")
            return None

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
        d = self._post("/api/Order/place", body)
        if not d.get("success"):
            raise RuntimeError(
                f"[ProjectX] order rejected: errorCode={d.get('errorCode')} {d.get('errorMessage')}"
            )
        order_id = str(d.get("orderId", ""))
        # Market orders fill near-instantly; ProjectX returns only the orderId on
        # placement (no avg fill price). We mark the entry at ref_price and let the
        # engine's reconcile step / quote stream true it up. get_fill confirms.
        return Fill(
            symbol=symbol, qty=float(n_contracts), side=side, price=ref_price,
            order_id=order_id, status="accepted",
        )

    def get_fill(self, order_id: str) -> tuple[str, float | None]:
        """Poll fill status. ProjectX market orders fill immediately; the place
        response carries no avg price, so we report filled at the provisional
        (ref) price (None). Exact avg-fill reconciliation would query
        /api/Trade/search by order — left as a future enhancement."""
        if self._mock_mode:
            return "filled", None
        return "filled", None

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
            return bool(d.get("success"))
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[ProjectX] cancel_order({order_id}) failed: {exc}")
            return False

    def get_positions(self) -> list[dict]:
        """Fetch open futures positions from ProjectX."""
        log.info(f"[ProjectX] get_positions() | mock={self._mock_mode}")
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
            log.warning(f"[ProjectX] get_positions() failed: {exc}")
            return []

    def get_account(self) -> dict:
        """Alias for account() — kept for compatibility."""
        return self.account()

    def flatten_all(self) -> None:
        """Close ALL open positions (EOD Topstep flatten rule) via closeContract."""
        log.info(f"[ProjectX] flatten_all() | mock={self._mock_mode}")
        if self._mock_mode:
            log.info("[ProjectX] MOCK flatten_all — no real orders placed")
            return
        try:
            for pos in self.get_positions():
                cid = pos.get("contract_id")
                if not cid:
                    continue
                try:
                    d = self._post("/api/Position/closeContract", {
                        "accountId": self.account_id, "contractId": cid,
                    })
                    ok = bool(d.get("success"))
                    log.info(f"[ProjectX] flatten {pos['symbol']} ({cid}): "
                             f"{'closed' if ok else 'FAILED ' + str(d.get('errorMessage'))}")
                except Exception as exc:  # noqa: BLE001
                    log.error(f"[ProjectX] closeContract({cid}) failed: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.error(f"[ProjectX] flatten_all() failed: {exc}")
