"""Execution agent. Turns a sized Signal into broker orders + Position rows.

Opens through whatever build_broker() returns (Alpaca / IBKR / Sim). In paper
or sim mode no real money moves. When CRAMER_MODE is on, every real entry also
opens an inverse shadow position (no broker order) so we can measure whether
flipping the signal would beat the strategy.

Futures sizing is RISK-BASED (not notional/price-based): qty is derived from a
per-trade dollar risk budget and the ATR-sized stop distance × the contract
$/point, so a single ES contract's true risk is checked against the Topstep
limits instead of always trading 1 lot. See futures_plan().
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from broker import build_broker
from config import CONFIG
from futures_symbols import dollar_value_per_point, is_futures_symbol
from signals import Signal
from state import Position, State

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flip(side: str) -> str:
    return "SELL" if side == "BUY" else "BUY"


# ── Risk-based futures sizing ────────────────────────────────────────────────
@dataclass(frozen=True)
class FuturesPlan:
    qty: int                    # contracts (≥1, ≤ TOPSTEP_MAX_CONTRACTS)
    stop_price: float           # native protective-stop price
    target_price: float         # take-profit (0.0 = none)
    stop_distance_points: float # ATR-sized stop distance in points
    point_value: float          # $ per point for the contract
    risk_usd: float             # worst-case loss at the stop = qty * dist * $/pt


def _futures_risk_budget_usd() -> float:
    """Max dollar risk for one futures trade: the SMALLER of (pct of account)
    and (fraction of the Daily Loss Limit). Keeps a single stop-out well inside
    both the trailing MLL and the DLL."""
    acct = CONFIG.topstep_account_size
    pct_cap = max(0.0, CONFIG.topstep_per_trade_risk_pct) * acct
    dll_cap = max(0.0, CONFIG.topstep_per_trade_risk_dll_frac) * CONFIG.topstep_daily_loss_limit
    caps = [c for c in (pct_cap, dll_cap) if c > 0]
    return min(caps) if caps else 0.0


def futures_plan(sig: Signal, price: float | None = None,
                 risk_mult: float = 1.0,
                 max_contracts: int | None = None) -> FuturesPlan | None:
    """Risk-based futures sizing + ATR bracket. Returns a plan, or None to REJECT
    the trade (invalid/zero ATR, unknown $/pt, or stop too wide for the budget).

    qty = floor(risk_mult × risk_budget / (stop_distance_pts × $/pt)), clamped to
    [1 .. cap]. A raw floor of 0 (stop too wide for the risk budget), or a cap of
    0 (no capacity left), REJECTS the trade — we never silently round up to
    1 contract, which would risk more than the budget / cap allows.

    risk_mult carries the engine's defensive multipliers (circuit breaker,
    regime, day-adapt) into the contract count. It is capped at 1.0 — the
    multipliers may only shrink the risk budget, never grow it.

    The cap defaults to TOPSTEP_MAX_CONTRACTS but callers pass a smaller value
    to respect the ACCOUNT-WIDE contract cap (remaining capacity =
    TOPSTEP_MAX_CONTRACTS − contracts already open across all symbols) — otherwise
    a single micro trade can size to the full cap and push the account total over
    the Topstep limit.
    """
    px = price if price is not None else sig.price
    pv = dollar_value_per_point(sig.symbol)
    atr = sig.atr
    if not (px and px > 0) or pv <= 0:
        return None
    # ATR must be finite and strictly positive — it sizes the stop.
    if not (atr and math.isfinite(atr) and atr > 0):
        return None
    dist = CONFIG.atr_stop_mult * atr            # points — NO equities % floor for futures
    if not (math.isfinite(dist) and dist > 0):
        return None
    per_contract_risk = dist * pv
    mult = min(1.0, risk_mult)
    if not (math.isfinite(mult) and mult > 0):
        return None
    budget = _futures_risk_budget_usd() * mult
    if per_contract_risk <= 0 or budget <= 0:
        return None
    cap = CONFIG.topstep_max_contracts if max_contracts is None else int(max_contracts)
    if cap < 1:
        return None                              # no account-wide capacity left → reject
    raw = math.floor(budget / per_contract_risk)
    if raw < 1:
        return None                              # stop too wide for the budget → reject
    qty = min(raw, cap)

    if CONFIG.take_profit_pct > 0:
        tdist = CONFIG.take_profit_pct * px
    else:
        tdist = CONFIG.atr_target_mult * atr if (atr and math.isfinite(atr)) else 0.0
    if sig.side == "BUY":
        stop = px - dist
        target = px + tdist if tdist > 0 else 0.0
    else:
        stop = px + dist
        target = px - tdist if tdist > 0 else 0.0
    return FuturesPlan(
        qty=qty, stop_price=stop, target_price=target,
        stop_distance_points=dist, point_value=pv,
        risk_usd=qty * per_contract_risk,
    )


class Executor:
    def __init__(self):
        self.broker = build_broker()
        self.mode = CONFIG.trading_mode

    def close_broker(self) -> None:
        self.broker.close()

    def get_fill(self, order_id: str) -> tuple[str, float | None]:
        return self.broker.get_fill(order_id)

    def open(self, sig: Signal, size_usd: float, state: State,
             risk_mult: float = 1.0,
             max_contracts: int | None = None) -> Position | None:
        """Size + submit an entry. Returns the Position, or None to REJECT (no
        broker order placed) when risk-based sizing yields 0 contracts or the
        stop is non-finite. risk_mult shrinks the futures risk budget (circuit
        breaker / regime / day-adapt) — capped at 1.0 inside futures_plan.

        `max_contracts` caps futures qty to the remaining ACCOUNT-WIDE capacity
        (TOPSTEP_MAX_CONTRACTS − contracts already open) so a new order can never
        push the total over the Topstep limit."""
        futures = is_futures_symbol(sig.symbol)
        if futures:
            plan = futures_plan(sig, sig.price, risk_mult=risk_mult,
                                max_contracts=max_contracts)
            if plan is None:
                log.warning("[exec] reject %s — futures sizing failed "
                            "(invalid ATR, stop too wide for risk budget, or no "
                            "account-wide contract capacity left)", sig.symbol)
                return None
            qty = plan.qty
        else:
            qty = max(1, int(size_usd / sig.price)) if sig.price > 0 else 0
            if qty < 1:
                return None
            # Validate the equities stop is finite BEFORE we send the order.
            if not math.isfinite(self._stop(sig, sig.price)):
                log.warning("[exec] reject %s — non-finite stop", sig.symbol)
                return None

        fill = self.broker.submit(sig.symbol, qty, sig.side, sig.price)
        filled = fill.status == "filled"

        # Re-anchor the bracket to the ACTUAL fill price (distances are unchanged).
        if futures:
            fp = futures_plan(sig, fill.price, risk_mult=risk_mult,
                              max_contracts=max_contracts) or plan
            stop, target = fp.stop_price, fp.target_price
        else:
            stop = self._stop(sig, fill.price)
            target = self._target(sig, fill.price)

        pos = Position(
            symbol=sig.symbol, asset=sig.asset, side=sig.side, qty=fill.qty,
            entry_price=fill.price, size_usd=fill.qty * fill.price,
            stop=stop, target=target,
            kind=sig.kind, thesis=sig.thesis, opened_at=_now(), mode=self.mode,
            order_id=fill.order_id, filled=filled,
        )
        state.add(pos)

        # ── Native exchange-resting protective stop (C1) ─────────────────────
        # Submit a STOP order on the opposite side immediately after the entry so a
        # crash/gap/disconnect can't leave the position unprotected against the
        # trailing MLL. Only the ProjectX broker exposes place_stop_order; Sim /
        # Alpaca skip it (no real-money futures). The id is stored so the
        # client-side exit path can cancel it (see close()).
        if futures and not pos.shadow and filled and stop and pos.qty > 0:
            place = getattr(self.broker, "place_stop_order", None)
            if callable(place):
                try:
                    stop_oid = place(sig.symbol, pos.qty, _flip(sig.side), stop)
                    pos.protective_order_id = stop_oid or ""
                    if pos.protective_order_id:
                        log.info("[exec] %s protective STOP resting @ %.2f (order %s)",
                                 sig.symbol, stop, pos.protective_order_id)
                    else:
                        log.error("[exec] %s protective STOP NOT confirmed — position "
                                  "relies on client-side stop only!", sig.symbol)
                except Exception as exc:  # noqa: BLE001
                    log.error("[exec] %s protective STOP submit failed: %s — "
                              "client-side stop only!", sig.symbol, exc)
                    pos.protective_order_id = ""

        # Cramer inverse shadow — bookkeeping only, never sent to broker.
        if CONFIG.cramer_mode and not state.has_open(sig.symbol, shadow=True):
            shadow_side = _flip(sig.side)
            state.add(Position(
                symbol=sig.symbol, asset=sig.asset, side=shadow_side, qty=fill.qty,
                entry_price=fill.price, size_usd=fill.qty * fill.price,
                stop=0.0, target=0.0, kind="cramer", thesis="inverse shadow",
                opened_at=_now(), mode=self.mode, shadow=True,
            ))
        return pos

    def close_partial(self, pos: Position, close_qty: int, exit_price: float) -> bool:
        """Close a partial qty of a futures position without fully closing it.

        Cancels the resting protective stop first (it will be re-placed at the
        new BE stop price by the caller), then submits the reduction order.
        Returns True on success. Caller is responsible for updating pos.qty and pos.stop.
        Shadow positions are bookkeeping-only — no broker order is placed.
        """
        if pos.shadow or close_qty <= 0:
            return False
        pid = getattr(pos, "protective_order_id", "")
        if pid:
            cancel = getattr(self.broker, "cancel_order", None)
            if callable(cancel):
                try:
                    cancel(pid)
                except Exception as exc:  # noqa: BLE001
                    log.warning("[exec] cancel protective stop %s for partial: %s", pid, exc)
            pos.protective_order_id = ""
        try:
            self.broker.submit(pos.symbol, close_qty, _flip(pos.side), exit_price)
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("[exec] partial close %s qty=%d failed: %s", pos.symbol, close_qty, exc)
            return False

    def replace_protective_stop(self, pos: Position, stop_price: float) -> None:
        """Re-arm the native resting stop for the remaining qty. Must be called
        after close_partial(), which cancels the original full-qty stop — the
        remainder would otherwise run naked against the trailing MLL."""
        if pos.shadow or pos.qty < 1 or not stop_price or not math.isfinite(stop_price):
            return
        place = getattr(self.broker, "place_stop_order", None)
        if not callable(place):
            return
        try:
            oid = place(pos.symbol, pos.qty, _flip(pos.side), stop_price)
            pos.protective_order_id = oid or ""
            if pos.protective_order_id:
                log.info("[exec] %s protective STOP re-armed @ %.2f (order %s)",
                         pos.symbol, stop_price, pos.protective_order_id)
            else:
                log.warning("[exec] %s protective stop re-arm returned no order id",
                            pos.symbol)
        except Exception as exc:  # noqa: BLE001
            log.error("[exec] %s protective stop re-arm failed: %s", pos.symbol, exc)

    def close(self, pos: Position, exit_price: float, state: State) -> None:
        if not pos.shadow:  # shadow book is paper-only bookkeeping
            # Cancel the native resting stop FIRST so it can't fill after we
            # flatten (which would re-open an unwanted opposite position).
            pid = getattr(pos, "protective_order_id", "")
            if pid:
                cancel = getattr(self.broker, "cancel_order", None)
                if callable(cancel):
                    try:
                        cancel(pid)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[exec] cancel protective stop %s failed: %s", pid, exc)
                pos.protective_order_id = ""
            self.broker.submit(pos.symbol, pos.qty, _flip(pos.side), exit_price)
        state.close(pos, exit_price)

    # ── ATR-based bracket levels (equities / non-futures path) ─────────────
    def _stop(self, sig: Signal, fill: float) -> float:
        # Futures size their stop in points via futures_plan (no % floor); this
        # equities path keeps the legacy percent-or-ATR stop. Returns NaN on a
        # non-finite result so open() can reject it rather than trade a dead stop.
        atr_stop = CONFIG.atr_stop_mult * sig.atr if sig.atr else 0.0
        pct_stop = CONFIG.stop_loss_pct * fill
        dist = max(atr_stop, pct_stop) if atr_stop else pct_stop
        stop = fill - dist if sig.side == "BUY" else fill + dist
        return stop if math.isfinite(stop) else float("nan")

    def _target(self, sig: Signal, fill: float) -> float:
        if CONFIG.take_profit_pct > 0:
            dist = CONFIG.take_profit_pct * fill
        else:
            dist = CONFIG.atr_target_mult * sig.atr if sig.atr else 0.0
        if not dist:
            return 0.0
        return fill + dist if sig.side == "BUY" else fill - dist


def build_executor() -> Executor:
    return Executor()
