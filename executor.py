"""Execution agent. Turns a sized Signal into broker orders + Position rows.

Opens through whatever build_broker() returns (Alpaca / IBKR / Sim). In paper
or sim mode no real money moves. When CRAMER_MODE is on, every real entry also
opens an inverse shadow position (no broker order) so we can measure whether
flipping the signal would beat the strategy.
"""
from __future__ import annotations

from datetime import datetime, timezone

from broker import build_broker
from config import CONFIG
from signals import Signal
from state import Position, State


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flip(side: str) -> str:
    return "SELL" if side == "BUY" else "BUY"


class Executor:
    def __init__(self):
        self.broker = build_broker()
        self.mode = CONFIG.trading_mode

    def close_broker(self) -> None:
        self.broker.close()

    def get_fill(self, order_id: str) -> tuple[str, float | None]:
        return self.broker.get_fill(order_id)

    def open(self, sig: Signal, size_usd: float, state: State) -> Position:
        qty = max(1, int(size_usd / sig.price)) if sig.price > 0 else 0
        fill = self.broker.submit(sig.symbol, qty, sig.side, sig.price)
        filled = fill.status == "filled"

        pos = Position(
            symbol=sig.symbol, asset=sig.asset, side=sig.side, qty=fill.qty,
            entry_price=fill.price, size_usd=fill.qty * fill.price,
            stop=self._stop(sig, fill.price), target=self._target(sig, fill.price),
            kind=sig.kind, thesis=sig.thesis, opened_at=_now(), mode=self.mode,
            order_id=fill.order_id, filled=filled,
        )
        state.add(pos)

        # Cramer inverse shadow — bookkeeping only, never sent to the broker.
        if CONFIG.cramer_mode and not state.has_open(sig.symbol, shadow=True):
            shadow_side = _flip(sig.side)
            state.add(Position(
                symbol=sig.symbol, asset=sig.asset, side=shadow_side, qty=fill.qty,
                entry_price=fill.price, size_usd=fill.qty * fill.price,
                stop=0.0, target=0.0, kind="cramer", thesis="inverse shadow",
                opened_at=_now(), mode=self.mode, shadow=True,
            ))
        return pos

    def close(self, pos: Position, exit_price: float, state: State) -> None:
        if not pos.shadow:  # shadow book is paper-only bookkeeping
            self.broker.submit(pos.symbol, pos.qty, _flip(pos.side), exit_price)
        state.close(pos, exit_price)

    # ── ATR-based bracket levels ──────────────────────────
    def _stop(self, sig: Signal, fill: float) -> float:
        atr_stop = CONFIG.atr_stop_mult * sig.atr if sig.atr else 0.0
        pct_stop = CONFIG.stop_loss_pct * fill
        dist = max(atr_stop, pct_stop) if atr_stop else pct_stop
        return fill - dist if sig.side == "BUY" else fill + dist

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
