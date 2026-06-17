"""Options analytics — the dealer-positioning "exposure stack".

Computes the structural map of the market from dealer hedging pressure:

    GEX  (Gamma Exposure)  — vol dampened (+GEX) vs amplified (-GEX)
    DEX  (Delta Exposure)  — directional anchor / magnet
    VEX  (Vanna Exposure)   — vol-down/market-up dynamic
    CHEX (Charm Exposure)   — time-decay flows into the close (0DTE)

From a per-strike exposure profile it derives the critical levels traders act
on: the Gamma Flip (zero gamma), and the Call/Put Walls (max-gamma strikes
that act as resistance/support).

Data is source-pluggable (OPTIONS_SOURCE):
  none        -> returns None (module dormant; equities still trade)
  flashalpha  -> pull pre-computed GEX/DEX from FlashAlpha MCP/REST  [TODO keys]
  chain       -> compute from a raw options chain (Alpaca)           [TODO Greeks]

Only the level math is fully implemented; the two data adapters are stubs with
explicit signatures so you can drop a feed in without touching callers.
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import date

import httpx

from config import CONFIG

_SQRT_2PI = math.sqrt(2.0 * math.pi)


def bs_gamma(spot: float, strike: float, t_years: float, iv: float, r: float = 0.0) -> float:
    """Black-Scholes gamma (same for calls/puts). Used as a fallback when a data
    source omits gamma but gives IV. Returns 0 on degenerate inputs."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    pdf = math.exp(-0.5 * d1 * d1) / _SQRT_2PI
    return pdf / (spot * iv * math.sqrt(t_years))


def _d1_d2(spot: float, strike: float, t_years: float, iv: float, r: float) -> tuple[float, float]:
    srt = iv * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / srt
    return d1, d1 - srt


def bs_delta(spot: float, strike: float, t_years: float, iv: float,
             is_call: bool, r: float = 0.0) -> float:
    """Black-Scholes delta. Fallback when a source omits delta but gives IV."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    d1, _ = _d1_d2(spot, strike, t_years, iv, r)
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return nd1 if is_call else nd1 - 1.0


def bs_vanna(spot: float, strike: float, t_years: float, iv: float, r: float = 0.0) -> float:
    """Vanna = ∂Δ/∂σ (identical for calls/puts). Per 1.00 change in vol."""
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, t_years, iv, r)
    pdf = math.exp(-0.5 * d1 * d1) / _SQRT_2PI
    return -pdf * d2 / iv


def bs_charm(spot: float, strike: float, t_years: float, iv: float, r: float = 0.0) -> float:
    """Charm = ∂Δ/∂τ (sensitivity of delta to time-to-expiry; same calls/puts, r=0).

    Positive τ-charm means delta rises as more time remains, i.e. decays toward 0
    as τ→0 — the source of 0DTE close-of-day delta-hedging flows (CHEX).
    """
    if spot <= 0 or strike <= 0 or iv <= 0 or t_years <= 0:
        return 0.0
    d1, d2 = _d1_d2(spot, strike, t_years, iv, r)
    pdf = math.exp(-0.5 * d1 * d1) / _SQRT_2PI
    return -pdf * d2 / (2.0 * t_years)

# FlashAlpha REST host (override with FLASHALPHA_BASE_URL). Historical replay host
# is historical.flashalpha.com + a required `at=YYYY-MM-DDTHH:mm:ss` param.
FLASHALPHA_BASE_URL = os.getenv("FLASHALPHA_BASE_URL", "https://lab.flashalpha.com")

# Free tier = 5 HTTP requests/DAY. Each profile fetch costs 2 (gex + levels), so the
# defaults below spend quota only on the regime symbol and refresh ~hourly. Raise these
# on a paid plan. FLASHALPHA_SYMBOLS empty -> fall back to the regime symbol only.
FLASHALPHA_TTL_SEC = int(os.getenv("FLASHALPHA_TTL_SEC", "3600"))
FLASHALPHA_DAILY_BUDGET = int(os.getenv("FLASHALPHA_DAILY_BUDGET", "5"))
_fa_syms_env = os.getenv("FLASHALPHA_SYMBOLS", "").strip()
FLASHALPHA_SYMBOLS = {s.strip().upper() for s in _fa_syms_env.split(",") if s.strip()} \
    or {CONFIG.regime_symbol.upper()}

# module state: per-symbol cache + a daily HTTP-call budget counter
_FA_CACHE: dict[str, tuple[float, "ExposureProfile | None"]] = {}
_FA_CALLS: dict[str, object] = {"date": "", "n": 0}


def _fa_budget_remaining() -> int:
    today = date.today().isoformat()
    if _FA_CALLS["date"] != today:           # new day resets the quota
        _FA_CALLS["date"], _FA_CALLS["n"] = today, 0
    return FLASHALPHA_DAILY_BUDGET - int(_FA_CALLS["n"])  # type: ignore[arg-type]


def _fa_spend(n: int) -> None:
    _FA_CALLS["n"] = int(_FA_CALLS["n"]) + n  # type: ignore[arg-type]


@dataclass
class StrikeExposure:
    strike: float
    gex: float = 0.0   # $ gamma per 1% move (dealer convention: + = long gamma)
    dex: float = 0.0   # $ delta
    vex: float = 0.0   # vanna
    chex: float = 0.0  # charm


@dataclass
class ExposureProfile:
    symbol: str
    spot: float
    strikes: list[StrikeExposure] = field(default_factory=list)
    # derived levels (filled by compute_levels)
    net_gex: float = 0.0
    net_dex: float = 0.0   # Σ dollar-delta of OI (signed by option delta; + = net long delta)
    net_vex: float = 0.0   # Σ vanna exposure (call +, put − convention, matching GEX)
    net_chex: float = 0.0  # Σ charm exposure (call +, put − convention, matching GEX)
    gamma_flip: float | None = None
    call_wall: float | None = None
    put_wall: float | None = None
    vol_trigger: float | None = None
    zero_dte_magnet: float | None = None  # strike 0DTE flows gravitate to (profit-target hint)
    front_expiry: "date | None" = None    # real listed front expiry (for valid OCC symbols)
    strike_step: float = 1.0              # real listed strike increment for this underlying

    @property
    def regime(self) -> str:
        """positive-gamma (dealers dampen) vs negative-gamma (dealers amplify)."""
        if self.net_gex > 0:
            return "positive-gamma"
        if self.net_gex < 0:
            return "negative-gamma"
        return "neutral"

    def near_wall(self, pct: float) -> str | None:
        """Return 'call_wall'/'put_wall' if spot sits within pct% of one."""
        for name, lvl in (("call_wall", self.call_wall), ("put_wall", self.put_wall)):
            if lvl and abs(self.spot - lvl) / self.spot * 100.0 <= pct:
                return name
        return None

    def summary(self) -> str:
        fl = f"{self.gamma_flip:.2f}" if self.gamma_flip else "—"
        cw = f"{self.call_wall:.2f}" if self.call_wall else "—"
        pw = f"{self.put_wall:.2f}" if self.put_wall else "—"
        return (f"{self.symbol} spot {self.spot:.2f} | {self.regime} "
                f"netGEX {self.net_gex:,.0f} | flip {fl} | callWall {cw} | putWall {pw}")


def compute_levels(p: ExposureProfile) -> ExposureProfile:
    """Derive gamma flip + walls from a populated strike profile.

    Gamma flip: the strike where cumulative GEX (ascending in strike) crosses
    zero — below it dealers are short gamma (amplify), above they're long
    (dampen). Walls: the strikes carrying the most positive gamma above (call
    wall = resistance) and below (put wall = support) spot.
    """
    rows = sorted(p.strikes, key=lambda s: s.strike)
    p.net_gex = sum(s.gex for s in rows)
    p.net_dex = sum(s.dex for s in rows)
    p.net_vex = sum(s.vex for s in rows)
    p.net_chex = sum(s.chex for s in rows)

    # cumulative-GEX zero crossing
    cum = 0.0
    prev_strike, prev_cum = None, 0.0
    for s in rows:
        cum += s.gex
        if prev_strike is not None and (prev_cum <= 0 < cum or prev_cum >= 0 > cum):
            # linear interpolate the crossing strike
            span = cum - prev_cum
            frac = (0 - prev_cum) / span if span else 0.0
            p.gamma_flip = prev_strike + frac * (s.strike - prev_strike)
            break
        prev_strike, prev_cum = s.strike, cum

    above = [s for s in rows if s.strike >= p.spot]
    below = [s for s in rows if s.strike <= p.spot]
    if above:
        p.call_wall = max(above, key=lambda s: s.gex).strike
    if below:
        p.put_wall = max(below, key=lambda s: s.gex).strike
    # Vol Trigger ≈ last positive-gamma support below spot (earlier warning than flip).
    pos_below = [s for s in below if s.gex > 0]
    if pos_below:
        p.vol_trigger = min(pos_below, key=lambda s: abs(s.strike - p.spot)).strike
    return p


@dataclass
class Confluence:
    """Result of the Four-Greek confluence read (v2 §4 tactical playbook)."""
    playbook: str | None          # gamma-squeeze | vanna-rally | charm-drift | gamma-pin | None
    direction: str | None         # bullish | bearish | neutral (neutral = sell premium)
    score: float                  # 0..1, share of the four greeks confirming `direction`
    note: str = ""

    @property
    def actionable(self) -> bool:
        return self.playbook is not None


def four_greek_confluence(p: ExposureProfile, *, zero_dte: bool = False) -> Confluence:
    """Classify the dealer-positioning regime into a tactical playbook.

    Priority order (most mechanically forced first):
      1. Charm Drift  — 0DTE only: net CHEX sign drives end-of-day delta unwind.
      2. Gamma Squeeze— negative GEX + spot pressing the call wall from below.
      3. Gamma Pin    — positive GEX + spot pinned at a wall → sell premium.
      4. Vanna Rally  — large positive VEX → mechanical buying on an IV crush.
    `score` = fraction of {GEX, DEX, VEX, CHEX} agreeing with the chosen direction,
    so a caller can gate on confluence strength, not just the trigger firing.
    """
    g = abs(p.net_gex) or 1.0
    big_vex = abs(p.net_vex) >= CONFIG.confluence_vex_threshold * g
    big_chex = abs(p.net_chex) >= CONFIG.confluence_chex_threshold * g

    def score_for(direction: str) -> float:
        bull = direction == "bullish"
        votes = [
            (p.net_gex < 0) == bull,        # neg gamma amplifies a bull move
            (p.net_dex > 0) == bull,        # net long dealer delta leans bull
            (p.net_vex > 0) == bull,        # positive vanna → up on vol-down
            (p.net_chex < 0) == bull,       # negative charm → bullish drift
        ]
        return round(sum(votes) / 4.0, 3)

    near = p.near_wall(CONFIG.wall_proximity_pct)

    # 1. Charm Drift (0DTE close-of-day mechanical unwind)
    if zero_dte and big_chex:
        direction = "bullish" if p.net_chex < 0 else "bearish"
        return Confluence("charm-drift", direction, score_for(direction),
                          note=f"0DTE charm {p.net_chex:,.0f}")

    # 2. Gamma Squeeze (negative gamma feedback toward the call wall)
    if p.net_gex < 0 and p.call_wall and p.spot < p.call_wall and near == "call_wall":
        return Confluence("gamma-squeeze", "bullish", score_for("bullish"),
                          note=f"spot {p.spot:.2f} < callWall {p.call_wall:.2f}, neg GEX")

    # 3. Gamma Pin (positive gamma damping at a wall → sell premium)
    if p.net_gex > 0 and near is not None:
        return Confluence("gamma-pin", "neutral", 0.0, note=f"pinned at {near}")

    # 4. Vanna Rally (large positive vanna; IV-crush trigger is the caller's macro filter)
    if big_vex and p.net_vex > 0:
        return Confluence("vanna-rally", "bullish", score_for("bullish"),
                          note=f"VEX {p.net_vex:,.0f} (needs IV-crush confirm)")

    return Confluence(None, None, 0.0, note="no confluence")


# ── data-source adapters ──────────────────────────────────

def exposure_for(symbol: str, spot: float, http: httpx.Client | None = None) -> ExposureProfile | None:
    """Return a computed ExposureProfile, or None when no source is configured."""
    src = CONFIG.options_source
    if src == "none":
        return None
    if src == "flashalpha":
        return _cached_flashalpha(symbol, spot, http)
    if src == "cboe":
        return _cached_cboe(symbol, spot, http)
    if src == "chain":
        return _from_chain(symbol, spot, http)
    return None


def _cached_flashalpha(symbol: str, spot: float, http: httpx.Client | None) -> ExposureProfile | None:
    """Quota-aware wrapper around _from_flashalpha for the 5-req/day free tier.

    Skips non-allowlisted symbols entirely, serves a TTL cache, and refuses to fetch
    once the daily HTTP budget is spent (returns the last cached value if any).
    """
    sym = symbol.upper()
    if sym not in FLASHALPHA_SYMBOLS:
        return None

    now = time.time()
    cached = _FA_CACHE.get(sym)
    if cached and now - cached[0] < FLASHALPHA_TTL_SEC:
        return cached[1]  # fresh enough — no network

    # A profile fetch is 2 HTTP calls (gex + levels). Don't start one we can't afford.
    if _fa_budget_remaining() < 2:
        return cached[1] if cached else None

    before = int(_FA_CALLS["n"])
    prof = _from_flashalpha(sym, spot, http)
    _ = before  # _from_flashalpha already incremented the counter per call
    _FA_CACHE[sym] = (now, prof)
    return prof


def _fa_strike_rows(gex_json: dict) -> list[StrikeExposure]:
    """Parse FlashAlpha /v1/exposure/gex per-strike payload into StrikeExposure rows.

    Handles both shapes the API returns:
      columnar  -> {"strikes":[...], "call_gex":[...], "put_gex":[...], "oi":[...]}
      row-wise  -> {"strikes":[{"strike":.., "call_gex":.., "put_gex":..}, ...]}
    put_gex is already signed negative by FlashAlpha (put wall = most-negative put gamma),
    so per-strike net gamma = call_gex + put_gex.
    """
    strikes = gex_json.get("strikes") or []
    rows: list[StrikeExposure] = []

    # row-wise: array of objects each carrying its own strike
    if strikes and isinstance(strikes[0], dict):
        for o in strikes:
            k = o.get("strike")
            if k is None:
                continue
            rows.append(StrikeExposure(
                strike=float(k),
                gex=float(o.get("call_gex", 0.0)) + float(o.get("put_gex", 0.0)),
            ))
        return rows

    # columnar: parallel arrays keyed alongside `strikes`
    call_gex = gex_json.get("call_gex") or []
    put_gex = gex_json.get("put_gex") or []
    for i, k in enumerate(strikes):
        cg = float(call_gex[i]) if i < len(call_gex) else 0.0
        pg = float(put_gex[i]) if i < len(put_gex) else 0.0
        rows.append(StrikeExposure(strike=float(k), gex=cg + pg))
    return rows


def _from_flashalpha(symbol: str, spot: float, http: httpx.Client | None) -> ExposureProfile | None:
    """Pull GEX + precomputed levels from FlashAlpha (lab.flashalpha.com).

    /v1/exposure/gex/{symbol}    -> per-strike call_gex/put_gex, net_gex, gamma_flip, as_of
    /v1/exposure/levels/{symbol} -> gamma_flip, call_wall, put_wall, zero_dte_magnet

    Strategy targets 0DTE, so we request `expiration=today`. Precomputed levels are
    preferred; we fall back to our own compute_levels() for anything the API omits.
    Best-effort: any failure returns None so equities keep trading.
    """
    key = CONFIG.flashalpha_api_key
    if not key:
        return None

    client = http or httpx.Client(timeout=10)
    headers = {"X-Api-Key": key}
    params = {"expiration": "today"}  # 0DTE; FlashAlpha free tier
    try:
        _fa_spend(1)  # count against the daily HTTP budget even if it errors
        gr = client.get(f"{FLASHALPHA_BASE_URL}/v1/exposure/gex/{symbol}",
                        headers=headers, params=params)
        gr.raise_for_status()
        gex_json = gr.json()
    except Exception:  # noqa: BLE001
        return None

    rows = _fa_strike_rows(gex_json)
    if not rows:
        return None

    prof = ExposureProfile(symbol=symbol, spot=spot, strikes=rows)
    prof = compute_levels(prof)  # fills net_gex, flip, walls, vol_trigger from the rows

    # Prefer the API's own net_gex / gamma_flip when present (it sees full chain depth).
    if "net_gex" in gex_json:
        try:
            prof.net_gex = float(gex_json["net_gex"])
        except (TypeError, ValueError):
            pass
    if gex_json.get("gamma_flip") is not None:
        try:
            prof.gamma_flip = float(gex_json["gamma_flip"])
        except (TypeError, ValueError):
            pass

    # Overlay precomputed key levels (call/put wall, 0DTE magnet) — best-effort.
    try:
        _fa_spend(1)  # second HTTP call against the daily budget
        lr = client.get(f"{FLASHALPHA_BASE_URL}/v1/exposure/levels/{symbol}",
                        headers=headers, params=params)
        lr.raise_for_status()
        lv = lr.json()
        for src, dst in (("gamma_flip", "gamma_flip"), ("call_wall", "call_wall"),
                         ("put_wall", "put_wall"), ("zero_dte_magnet", "zero_dte_magnet")):
            if lv.get(src) is not None:
                setattr(prof, dst, float(lv[src]))
    except Exception:  # noqa: BLE001
        pass  # levels are an enhancement; gex endpoint already gave us a usable profile

    return prof


# ── CBOE free delayed quotes (no key, no rate limit) ──────────────
# One payload carries per-contract bid/ask + full greeks + OI + IV. ~15min delayed, but
# GEX runs on OI (daily) and paper-marking tolerates the delay. We parse the WHOLE front
# expiry into a CboeChain so the engine/executor can price, size, and liquidity-filter
# real option legs — not just read GEX levels.
CBOE_BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options"
# Indices are prefixed with an underscore on CBOE (e.g. _SPX, _VIX). ETFs are plain.
_CBOE_INDEX = {"SPX", "NDX", "VIX", "RUT", "DJX", "XSP"}
_CBOE_TTL_SEC = int(os.getenv("CBOE_TTL_SEC", "300"))   # CDN-friendly; 5min cache
_CBOE_CACHE: dict[str, tuple[float, "CboeChain | None"]] = {}


def _parse_occ(occ: str) -> tuple[str, "date", bool, float] | None:
    """Parse an OCC symbol -> (root, expiry, is_call, strike). None if malformed.
    Layout (from the right): 8-digit strike*1000, 1 char C/P, 6-digit YYMMDD, root."""
    try:
        strike = int(occ[-8:]) / 1000.0
        is_call = occ[-9].upper() == "C"
        ymd = occ[-15:-9]
        root = occ[:-15]
        expiry = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
    except (ValueError, IndexError):
        return None
    if not root:
        return None
    return root, expiry, is_call, strike


@dataclass
class ContractQuote:
    occ: str
    strike: float
    is_call: bool
    expiry: "date"
    bid: float
    ask: float
    oi: float
    volume: float
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2, 4)
        return self.ask or self.bid or 0.0

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else 1.0

    def liquid(self, max_spread_pct: float, min_mid: float, max_abs_spread: float = 0.10) -> bool:
        if self.mid < min_mid or self.bid <= 0 or self.ask <= 0:
            return False
        # cheap protective legs have a wide % spread but a tiny absolute one — accept either.
        return self.spread_pct <= max_spread_pct or (self.ask - self.bid) <= max_abs_spread


@dataclass
class CboeChain:
    symbol: str
    spot: float
    front_expiry: "date"
    strike_step: float
    by_occ: dict[str, ContractQuote] = field(default_factory=dict)
    strikes: list[float] = field(default_factory=list)  # sorted listed strikes (front expiry)

    def nearest_strike(self, price: float) -> float:
        return min(self.strikes, key=lambda k: abs(k - price)) if self.strikes else price

    def get_occ(self, occ: str) -> "ContractQuote | None":
        return self.by_occ.get(occ)


def cboe_chain(symbol: str, spot: float, http: httpx.Client | None = None) -> "CboeChain | None":
    """Fetch + parse the full CBOE front-expiry chain (cached). None on failure."""
    sym = symbol.upper()
    now = time.time()
    hit = _CBOE_CACHE.get(sym)
    if hit and now - hit[0] < _CBOE_TTL_SEC:
        return hit[1]

    cboe_sym = f"_{sym}" if sym in _CBOE_INDEX else sym
    client = http or httpx.Client(timeout=12)
    try:
        r = client.get(f"{CBOE_BASE}/{cboe_sym}.json", headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
    except Exception:  # noqa: BLE001
        _CBOE_CACHE[sym] = (now, None)
        return None

    rows_raw = data.get("options") or []
    if not rows_raw:
        _CBOE_CACHE[sym] = (now, None)
        return None
    use_spot = float(data.get("current_price") or spot) or spot

    today = date.today()
    parsed = []
    for o in rows_raw:
        occ = o.get("option")
        pq = _parse_occ(occ) if occ else None
        if pq and pq[1] >= today:
            parsed.append((pq, o))
    if not parsed:
        _CBOE_CACHE[sym] = (now, None)
        return None
    front_exp = min(p[0][1] for p in parsed)

    by_occ: dict[str, ContractQuote] = {}
    strikes: set[float] = set()
    for (root, expiry, is_call, strike), o in parsed:
        if expiry != front_exp:
            continue
        by_occ[o["option"]] = ContractQuote(
            occ=o["option"], strike=strike, is_call=is_call, expiry=expiry,
            bid=float(o.get("bid") or 0.0), ask=float(o.get("ask") or 0.0),
            oi=float(o.get("open_interest") or 0.0), volume=float(o.get("volume") or 0.0),
            delta=float(o.get("delta") or 0.0), gamma=float(o.get("gamma") or 0.0),
            theta=float(o.get("theta") or 0.0), vega=float(o.get("vega") or 0.0),
            iv=float(o.get("iv") or 0.0),
        )
        strikes.add(strike)

    sorted_strikes = sorted(strikes)
    step = 1.0
    if len(sorted_strikes) > 1:
        diffs = [round(b - a, 4) for a, b in zip(sorted_strikes, sorted_strikes[1:]) if b > a]
        if diffs:
            step = min(diffs)  # smallest gap = the listed increment
    chain = CboeChain(symbol=sym, spot=use_spot, front_expiry=front_exp,
                      strike_step=step, by_occ=by_occ, strikes=sorted_strikes)
    _CBOE_CACHE[sym] = (now, chain)
    return chain


# ── effective open-interest calibration (v2 §2) ───────────────────────────────
# OI only prints once a day, so intraday GEX from raw OI is stale. We snapshot the
# day's first-seen OI per contract and add a confidence-weighted slice of intraday
# volume, then scale by a bounded daily residual recalibrated against the next day's
# observed OI. Disabled by default (CONFIG.effective_oi_enabled) — see config note.
_OI_SNAPSHOT: dict[str, dict[tuple, float]] = {}   # "SYM:DATE" -> {(strike,is_call): first_oi}
_OI_RESIDUAL: dict[str, float] = {}                # symbol -> bounded multiplier
_OI_PREV_MODELED: dict[str, tuple[str, float]] = {}  # symbol -> (date, modeled_total)


def effective_oi(symbol: str, q: "ContractQuote", today_str: str) -> float:
    """Estimated effective OI for a contract. Raw OI when the feature is off."""
    if not CONFIG.effective_oi_enabled:
        return q.oi
    key = f"{symbol}:{today_str}"
    snap = _OI_SNAPSHOT.setdefault(key, {})
    k = (q.strike, q.is_call)
    snap.setdefault(k, q.oi)                         # freeze the open snapshot
    est = snap[k] + CONFIG.effective_oi_weight * q.volume
    return est * _OI_RESIDUAL.get(symbol, 1.0)


def _update_oi_residual(symbol: str, today_str: str, observed_total: float,
                        modeled_total: float) -> None:
    """Once per new day, calibrate the residual: yesterday's modeled OI vs today's
    observed OI. Clamped to [1-cap, 1+cap] so a bad day can't blow up sizing."""
    prev = _OI_PREV_MODELED.get(symbol)
    if prev and prev[0] != today_str and prev[1] > 0:
        ratio = observed_total / prev[1]
        cap = CONFIG.effective_oi_residual_cap
        _OI_RESIDUAL[symbol] = max(1.0 - cap, min(1.0 + cap, ratio))
    if not prev or prev[0] != today_str:
        _OI_PREV_MODELED[symbol] = (today_str, modeled_total)


def _from_cboe(symbol: str, spot: float, http: httpx.Client | None) -> ExposureProfile | None:
    """Build the full GEX/DEX/VEX/CHEX ExposureProfile from the cached CBOE chain.

    Per-strike signed GEX = gamma*OI*100*spot^2*0.01 (call +, put -). DEX uses the
    option's natural (signed) delta; VEX/CHEX use BS vanna/charm (the chain has no
    higher-order greeks) under the same call +/put − convention as GEX. Falls back
    to BS gamma/delta when the feed omits them but gives IV. OI is the effective-OI
    estimate when that feature is enabled.
    """
    chain = cboe_chain(symbol, spot, http)
    if chain is None:
        return None
    today = date.today()
    today_str = today.isoformat()
    t = max((chain.front_expiry - today).days, 0) / 365.0 or (1.0 / (365 * 24))
    s2 = chain.spot * chain.spot * 0.01
    dollar = chain.spot * 100.0
    rows: dict[float, StrikeExposure] = {}
    obs_oi_total = mdl_oi_total = 0.0
    for q in chain.by_occ.values():
        obs_oi_total += q.oi
        oi = effective_oi(symbol, q, today_str)
        if oi <= 0:
            continue
        mdl_oi_total += oi
        gamma = q.gamma or (bs_gamma(chain.spot, q.strike, t, q.iv) if q.iv > 0 else 0.0)
        delta = q.delta or (bs_delta(chain.spot, q.strike, t, q.iv, q.is_call) if q.iv > 0 else 0.0)
        vanna = bs_vanna(chain.spot, q.strike, t, q.iv) if q.iv > 0 else 0.0
        charm = bs_charm(chain.spot, q.strike, t, q.iv) if q.iv > 0 else 0.0
        sign = 1.0 if q.is_call else -1.0
        r = rows.setdefault(q.strike, StrikeExposure(strike=q.strike))
        r.gex += sign * gamma * oi * 100.0 * s2
        r.dex += delta * oi * dollar                 # delta already signed call+/put−
        r.vex += sign * vanna * oi * dollar
        r.chex += sign * charm * oi * dollar

    if not rows:
        return None
    if CONFIG.effective_oi_enabled:
        _update_oi_residual(symbol, today_str, obs_oi_total, mdl_oi_total)
    prof = compute_levels(ExposureProfile(symbol=chain.symbol, spot=chain.spot,
                                          strikes=sorted(rows.values(), key=lambda s: s.strike)))
    prof.front_expiry = chain.front_expiry
    prof.strike_step = chain.strike_step
    return prof


def _cached_cboe(symbol: str, spot: float, http: httpx.Client | None) -> ExposureProfile | None:
    """ExposureProfile from CBOE (chain itself is cached inside cboe_chain)."""
    return _from_cboe(symbol, spot, http)


def _from_chain(symbol: str, spot: float, http: httpx.Client | None) -> ExposureProfile | None:
    """Compute dealer GEX from a raw options chain (Alpaca options).

    TODO: pull the chain (strikes, open interest, IV), compute per-contract
    gamma via Black-Scholes, scale by OI and 100x contract multiplier, sign by
    the dealer-short-calls / dealer-long-puts convention, then compute_levels().
    """
    return None
