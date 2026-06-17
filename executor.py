"""Execution agent. Turns a sized Signal into broker orders + Position rows.

Opens through whatever build_broker() returns (Alpaca / IBKR / Sim). In paper
or sim mode no real money moves. When CRAMER_MODE is on, every real entry also
opens an inverse shadow position (no broker order) so we can measure whether
flipping the signal would beat the strategy.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from broker import SimBroker, build_broker
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
        # Index options (SPX/XSP) can't route to Alpaca — fill them on the internal
        # simulator (premium-based, marked off the real CBOE chain at close).
        self._sim = SimBroker() if self.broker.name != "sim" else self.broker
        self.mode = CONFIG.trading_mode

    def _option_broker(self, symbol: str):
        return self._sim if CONFIG.is_index(symbol) else self.broker

    def close_broker(self) -> None:
        self.broker.close()

    def get_fill(self, order_id: str) -> tuple[str, float | None]:
        return self.broker.get_fill(order_id)

    def open(self, sig: Signal, size_usd: float, state: State) -> Position:
        if sig.asset == "option" and sig.structure is not None:
            return self.open_option(sig, size_usd, state)
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

    def open_option(self, sig: Signal, size_usd: float, state: State) -> Position:
        """Open a regime-adaptive 0DTE structure. Sized by REAL per-contract risk
        (premium for debit, width-credit for spreads) from the live chain. Positions
        track underlying for exit triggers (target=magnet, stop=flip) but P&L is marked
        by real option premium (entry_value -> current net mid at close).
        """
        st = sig.structure
        risk = st.max_loss if st.max_loss > 0 else CONFIG.option_nominal_premium * 100
        qty = max(1, int(size_usd / risk))
        fill = self._option_broker(sig.symbol).submit_option(st, qty, sig.price, opening=True)
        envelope = json.dumps({
            "legs": [{"occ": l.occ, "side": l.side, "is_call": l.is_call,
                      "strike": l.strike, "ratio": l.ratio} for l in st.legs],
            "entry_value": st.entry_value, "is_debit": st.is_debit,
        })
        pos = Position(
            symbol=sig.symbol, asset="option", side=sig.side, qty=fill.qty,
            entry_price=sig.price, size_usd=round(qty * risk, 2),
            stop=sig.stop or 0.0, target=st.target or 0.0,
            kind=st.kind, thesis=st.thesis or sig.thesis, opened_at=_now(),
            mode=self.mode, order_id=fill.order_id,
            filled=(fill.status == "filled"), contract=envelope,
        )
        state.add(pos)
        return pos

    def close(self, pos: Position, exit_price: float, state: State) -> None:
        if pos.asset == "option" and pos.contract:
            pnl = self._close_option(pos)        # submits closing order + returns premium P&L
            state.close(pos, exit_price, pnl_override=pnl)
            return
        if not pos.shadow:  # shadow book is paper-only bookkeeping
            self.broker.submit(pos.symbol, pos.qty, _flip(pos.side), exit_price)
        state.close(pos, exit_price)

    def _close_option(self, pos: Position) -> float | None:
        """Submit the closing order; return premium-based P&L = (current-entry)*100*qty.

        P&L is computed on the structure AS HELD (legs as written), so it's correct for
        both debit (long) and credit (short) without branching. None if no current quote.
        """
        from options import cboe_chain
        from options_strategy import OptionLeg, OptionStructure
        env = json.loads(pos.contract)
        legs = [OptionLeg(occ=d["occ"], side=d["side"], is_call=d["is_call"],
                          strike=d["strike"], ratio=d.get("ratio", 1))
                for d in env["legs"]]
        st = OptionStructure(kind=pos.kind, legs=legs, is_debit=env.get("is_debit", True))
        if not pos.shadow:
            self._option_broker(pos.symbol).submit_option(st, pos.qty, pos.entry_price, opening=False)

        chain = cboe_chain(pos.symbol, pos.entry_price)
        if chain is None:
            return None
        current = 0.0
        for lg in legs:
            q = chain.get_occ(lg.occ)
            if q is None:
                return None
            current += q.mid if lg.side == "buy" else -q.mid
        return round((current - env.get("entry_value", 0.0)) * 100 * pos.qty, 2)

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
