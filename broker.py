"""Broker adapters: a thin common interface over execution venues.

    AlpacaBroker — REST against the paper or live endpoint (alpaca-py or raw httpx)
    IBKRBroker   — stub for later multi-asset (stocks/options/futures)
    SimBroker    — internal fill simulator, no network (default fallback)

All three implement:  submit(symbol, qty, side) -> Fill   and   account() -> dict
Higher layers (executor.py) never import a concrete broker — they call
build_broker(). Live orders only go out when TRADING_MODE=live.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from config import CONFIG


@dataclass
class Fill:
    symbol: str
    qty: float
    side: str            # "BUY" | "SELL"
    price: float
    order_id: str = ""
    status: str = "filled"


class SimBroker:
    """Optimistic paper fills at the reference price. Zero network."""
    name = "sim"

    def submit(self, symbol: str, qty: float, side: str, ref_price: float) -> Fill:
        return Fill(symbol=symbol, qty=qty, side=side, price=ref_price,
                    order_id="sim", status="filled")

    def get_fill(self, order_id: str) -> tuple[str, float | None]:
        return "filled", None  # sim fills are final at submit

    def submit_option(self, structure, qty: float, ref_price: float, opening: bool = True) -> Fill:
        primary = structure.legs[0].occ if structure.legs else structure.kind
        return Fill(symbol=primary, qty=max(1, int(qty)),
                    side="BUY" if opening else "SELL", price=ref_price,
                    order_id="sim", status="filled")

    def account(self) -> dict:
        return {"cash": CONFIG.bankroll_usd, "equity": CONFIG.bankroll_usd, "source": "sim"}

    def close(self) -> None:
        pass


class AlpacaBroker:
    """Alpaca REST. Paper endpoint unless TRADING_MODE=live."""
    name = "alpaca"

    def __init__(self):
        self._http = httpx.Client(
            base_url=CONFIG.alpaca_base_url,
            timeout=15.0,
            headers={
                "APCA-API-KEY-ID": CONFIG.alpaca_api_key,
                "APCA-API-SECRET-KEY": CONFIG.alpaca_secret_key,
            },
        )

    def submit(self, symbol: str, qty: float, side: str, ref_price: float) -> Fill:
        # Market order, day TIF. qty rounded to whole shares (fractional needs notional).
        body = {
            "symbol": symbol,
            "qty": str(max(1, int(qty))),
            "side": side.lower(),
            "type": "market",
            "time_in_force": "day",
        }
        r = self._http.post("/v2/orders", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"alpaca order rejected {r.status_code}: {r.text}")
        d = r.json()
        filled = d.get("filled_avg_price")
        return Fill(
            symbol=symbol, qty=float(d.get("qty", qty)), side=side,
            price=float(filled) if filled else ref_price,
            order_id=str(d.get("id", "")), status=str(d.get("status", "accepted")),
        )

    def submit_option(self, structure, qty: float, ref_price: float, opening: bool = True) -> Fill:
        """Place a 0DTE option order. Single leg -> simple order; spread -> mleg order.

        `opening` False flips each leg's side (buy<->sell) to close the structure.
        Market order, day TIF. ref_price is the UNDERLYING spot (we mark/track by the
        underlying, not the option premium — see executor for why).
        """
        n = max(1, int(qty))

        def leg_side(buy: bool) -> str:
            buy = buy if opening else (not buy)
            return "buy" if buy else "sell"

        def intent(buy: bool) -> str:
            buy = buy if opening else (not buy)
            oc = "open" if opening else "close"
            return f"{'buy' if buy else 'sell'}_to_{oc}"

        if len(structure.legs) == 1:
            lg = structure.legs[0]
            body = {"symbol": lg.occ, "qty": str(n), "side": leg_side(lg.side == "buy"),
                    "type": "market", "time_in_force": "day"}
        else:
            body = {
                "order_class": "mleg", "qty": str(n), "type": "market", "time_in_force": "day",
                "legs": [{"symbol": lg.occ, "ratio_qty": str(lg.ratio),
                          "side": leg_side(lg.side == "buy"),
                          "position_intent": intent(lg.side == "buy")}
                         for lg in structure.legs],
            }
        r = self._http.post("/v2/orders", json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"alpaca option order rejected {r.status_code}: {r.text}")
        d = r.json()
        primary = structure.legs[0].occ if structure.legs else structure.kind
        return Fill(symbol=primary, qty=float(n), side="BUY" if opening else "SELL",
                    price=ref_price, order_id=str(d.get("id", "")),
                    status=str(d.get("status", "accepted")))

    def get_fill(self, order_id: str) -> tuple[str, float | None]:
        """Return (status, filled_avg_price). price is None until filled."""
        try:
            r = self._http.get(f"/v2/orders/{order_id}")
            r.raise_for_status()
            d = r.json()
        except Exception:  # noqa: BLE001
            return "unknown", None
        price = d.get("filled_avg_price")
        return str(d.get("status", "unknown")), (float(price) if price else None)

    def account(self) -> dict:
        try:
            r = self._http.get("/v2/account")
            r.raise_for_status()
            d = r.json()
            return {"cash": float(d["cash"]), "equity": float(d["equity"]),
                    "source": "alpaca", "buying_power": float(d.get("buying_power", 0))}
        except Exception as e:  # noqa: BLE001
            return {"cash": None, "equity": None, "source": f"alpaca-error: {e}"}

    def close(self) -> None:
        self._http.close()


class IBKRBroker:
    """Interactive Brokers adapter — stub for later multi-asset execution.

    TODO: implement via ib_insync against TWS/IB Gateway (IBKR_HOST/PORT/
    CLIENT_ID). Left as an explicit interface so equities can move to IBKR and
    options/futures get a venue without changing callers.
    """
    name = "ibkr"

    def __init__(self):
        raise NotImplementedError(
            "IBKR adapter not implemented yet — set BROKER=alpaca (or sim). "
            "Wire ib_insync against TWS/Gateway to enable."
        )

    def submit(self, symbol: str, qty: float, side: str, ref_price: float) -> Fill:  # pragma: no cover
        raise NotImplementedError

    def get_fill(self, order_id: str) -> tuple[str, float | None]:  # pragma: no cover
        raise NotImplementedError

    def account(self) -> dict:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


def build_broker():
    if CONFIG.broker == "alpaca":
        return AlpacaBroker()
    if CONFIG.broker == "ibkr":
        return IBKRBroker()
    return SimBroker()
