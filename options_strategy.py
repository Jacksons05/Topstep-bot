"""Regime-adaptive 0DTE option structure selection (the options-primary scalper brain).

Maps a live GEX regime + a directional lean to a concrete option structure with strikes,
anchored to the dealer-positioning levels (walls / flip / 0DTE magnet). Pure logic — no
network, no broker — so it is fully unit-tested. Execution (Alpaca option orders) and
intraday data live elsewhere; this module only decides WHAT to trade.

Regime → structure (from the strategy research):
  NEGATIVE gamma (trending / "slippery")  -> LONG single-leg ATM 0DTE (debit); delta
                                             gains outpace theta on the breakout.
  POSITIVE gamma (sticky / mean-reverting) -> CREDIT spread; sell premium with the short
                                             leg parked at the wall to ride the pin.

Direction (BUY = bullish, SELL = bearish) then picks call vs put / bull vs bear.
Profit target for every structure = the 0DTE magnet (falls back to the opposing wall).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from options import ExposureProfile


def occ_symbol(underlying: str, expiry: date, is_call: bool, strike: float) -> str:
    """Build a standard OCC option symbol, e.g. SPY + 2026-06-27 + put + 400 ->
    'SPY260627P00400000'. Strike is encoded as price*1000, zero-padded to 8 digits."""
    root = underlying.upper()
    ymd = expiry.strftime("%y%m%d")
    cp = "C" if is_call else "P"
    strike_int = int(round(strike * 1000))
    return f"{root}{ymd}{cp}{strike_int:08d}"


@dataclass(frozen=True)
class OptionLeg:
    occ: str
    side: str          # "buy" | "sell"
    is_call: bool
    strike: float
    ratio: int = 1     # contracts per 1 unit of structure size


@dataclass
class OptionStructure:
    kind: str          # long_call | long_put | bull_put | bear_call
    legs: list[OptionLeg] = field(default_factory=list)
    is_debit: bool = True       # debit (pay) vs credit (collect)
    target: float | None = None # underlying price profit target (0DTE magnet)
    thesis: str = ""
    # filled by price_structure() against a live chain:
    entry_value: float = 0.0    # net mid premium per contract (+ = you pay, - = you collect)
    max_loss: float = 0.0       # $ at risk per 1 structure (premium for debit; width-credit for spread)

    @property
    def is_multi_leg(self) -> bool:
        return len(self.legs) > 1


def directional_levels(direction: str, spot: float, exposure, atr: float) -> tuple[float, float]:
    """Pick (stop, target) UNDERLYING levels on the correct side of spot for the trade.

    The magnet/flip/walls can sit on either side of spot; using them blindly makes a
    position exit the instant it opens (target already 'hit'). This guarantees:
      bullish -> target ABOVE spot, stop BELOW spot
      bearish -> target BELOW spot, stop ABOVE spot
    Falls back to walls then an ATR offset when a level is on the wrong side.
    """
    bullish = direction == "BUY"
    a = atr if atr > 0 else spot * 0.003   # ~0.3% if ATR missing
    mag, flip = exposure.zero_dte_magnet, exposure.gamma_flip
    cw, pw, vt = exposure.call_wall, exposure.put_wall, exposure.vol_trigger

    def first_above(*cands):
        for c in cands:
            if c is not None and c > spot:
                return c
        return spot + 1.5 * a

    def first_below(*cands):
        for c in cands:
            if c is not None and c < spot:
                return c
        return spot - 1.5 * a

    if bullish:
        return first_below(flip, vt, pw), first_above(mag, cw)      # (stop, target)
    return first_above(flip, vt, cw), first_below(mag, pw)          # (stop, target)


def price_structure(chain, structure: "OptionStructure", *, spread_width: float = 1.0,
                    max_spread_pct: float = 0.20, min_mid: float = 0.05,
                    max_abs_spread: float = 0.10) -> bool:
    """Price + liquidity-validate a structure against a live CboeChain.

    Every leg must exist in the chain and pass the liquidity filter (spread/mid). On
    success, sets structure.entry_value (net mid, + = debit paid) and structure.max_loss
    ($/contract at risk) and returns True. Returns False if any leg is missing/illiquid.
    """
    net_mid = 0.0
    for lg in structure.legs:
        q = chain.get_occ(lg.occ)
        if q is None or not q.liquid(max_spread_pct, min_mid, max_abs_spread):
            return False
        net_mid += q.mid if lg.side == "buy" else -q.mid   # buy pays, sell collects
    structure.entry_value = round(net_mid, 4)

    if structure.is_debit:
        structure.max_loss = round(abs(net_mid) * 100, 2)          # debit = premium paid
    else:
        credit = -net_mid                                          # collected (net_mid is negative)
        structure.max_loss = round(max(spread_width - credit, 0.01) * 100, 2)  # spread max loss
    return True


def _round_to_step(price: float, step: float) -> float:
    return round(round(price / step) * step, 2)


def select_structure(
    exposure: ExposureProfile,
    direction: str,          # "BUY" (bullish) | "SELL" (bearish)
    expiry: date,
    *,
    strike_step: float = 1.0,
    spread_width: float = 1.0,
    iv_rank: float | None = None,
    iv_rank_sell_threshold: float = 50.0,
    iv_rank_buy_threshold: float = 30.0,
) -> OptionStructure | None:
    """Pick a 0DTE structure for the current regime + lean, optionally biased by IV rank.

    IV rank override (when iv_rank is not None):
      > iv_rank_sell_threshold  → elevated vol, prefer selling premium (credit spread)
      < iv_rank_buy_threshold   → cheap vol, prefer buying debit (long single-leg)
      in between                → let the GEX regime decide (original logic)

    The override maps to the same structures as the regime path — debit vs credit — so
    the rest of the pricing / liquidity pipeline is unchanged. The thesis string records
    which signal drove the structure choice.

    Returns None if direction is flat or regime is neutral with no IV-rank override.
    """
    if direction not in ("BUY", "SELL"):
        return None

    spot = exposure.spot
    sym = exposure.symbol
    bullish = direction == "BUY"
    target = exposure.zero_dte_magnet
    if target is None:  # fall back to the wall we're aiming at
        target = exposure.call_wall if bullish else exposure.put_wall

    # ── IV rank override: may flip debit<->credit vs the pure regime read ──
    regime = exposure.regime
    iv_tag = ""
    if iv_rank is not None:
        if iv_rank > iv_rank_sell_threshold:
            regime = "positive-gamma"   # elevated vol → sell premium
            iv_tag = f" [IVR {iv_rank:.0f}↑ sell]"
        elif iv_rank < iv_rank_buy_threshold:
            regime = "negative-gamma"   # cheap vol → buy debit
            iv_tag = f" [IVR {iv_rank:.0f}↓ buy]"
        # else: IV rank is middling; trust the GEX regime

    # ── NEGATIVE gamma path: trending / cheap-vol → long single-leg ATM (debit) ──
    if regime == "negative-gamma":
        is_call = bullish
        strike = _round_to_step(spot, strike_step)  # ATM
        leg = OptionLeg(occ_symbol(sym, expiry, is_call, strike), "buy", is_call, strike)
        return OptionStructure(
            kind="long_call" if is_call else "long_put",
            legs=[leg], is_debit=True, target=target,
            thesis=(
                f"neg-gamma momentum: long {'call' if is_call else 'put'} ATM {strike}{iv_tag}"
            ),
        )

    # ── POSITIVE gamma path: sticky / elevated-vol → credit spread, short at the wall ──
    if regime == "positive-gamma":
        if bullish:
            # bull put spread: sell put at/just below put wall, buy put one width lower
            short_k = _round_to_step(exposure.put_wall or spot, strike_step)
            long_k = _round_to_step(short_k - spread_width, strike_step)
            legs = [
                OptionLeg(occ_symbol(sym, expiry, False, short_k), "sell", False, short_k),
                OptionLeg(occ_symbol(sym, expiry, False, long_k), "buy", False, long_k),
            ]
            return OptionStructure(
                "bull_put", legs, is_debit=False, target=target,
                thesis=f"pos-gamma pin: bull put {short_k}/{long_k} below put wall{iv_tag}",
            )
        else:
            # bear call spread: sell call at/just above call wall, buy call one width higher
            short_k = _round_to_step(exposure.call_wall or spot, strike_step)
            long_k = _round_to_step(short_k + spread_width, strike_step)
            legs = [
                OptionLeg(occ_symbol(sym, expiry, True, short_k), "sell", True, short_k),
                OptionLeg(occ_symbol(sym, expiry, True, long_k), "buy", True, long_k),
            ]
            return OptionStructure(
                "bear_call", legs, is_debit=False, target=target,
                thesis=f"pos-gamma pin: bear call {short_k}/{long_k} above call wall{iv_tag}",
            )

    return None  # neutral regime + middling IV rank → no structure
