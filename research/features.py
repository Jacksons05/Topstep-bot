"""Feature engineering for pre-registered rounds — every feature the account
holder listed, computed from data already owned.

CAUSALITY IS THE CONTRACT. Every function here is safe to call at bar i using
only information available at or before bar i's close, or is explicitly a
PRIOR-SESSION value settled before today opened. Nothing reads the future.
The Round-24 post-mortem is the reason this module exists: fill/lookahead
mistakes are what manufacture fake edges, so the primitives are written once,
carefully, and reused.

Feature groups
  bar/vol    : ATR, realized vol, ATR-percentile volatility regime
  session    : prior-day H/L/MID, opening range, initial balance, VWAP distance
  profile    : volume-profile POC / VAH / VAL (market-profile value area)
  overnight  : overnight move, inventory skew, gap vs prior close
  dealer     : GEX level/sign/regime (SqueezeMetrics daily, causal-shifted)
  micro      : 1-second book imbalance / spread / iceberg pressure from the
               nightly $0 GLBX MBO reduction
  calendar   : time-of-day, day-of-week, session phase
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, timedelta

import numpy as np

from . import datasets as ds

PX_SCALE = 1e-9          # Databento fixed-precision price units


# ── bar / volatility ─────────────────────────────────────────────────────────

def atr(h, l, c, period: int = 14) -> np.ndarray:
    """Wilder-style simple-mean ATR; NaN until `period` bars exist."""
    n = len(c)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    pc = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    cs = np.cumsum(np.insert(tr, 0, 0.0))
    out[period - 1:] = (cs[period:] - cs[:-period]) / period
    return out


def realized_vol(c, i: int, n: int = 20) -> float:
    """Std-dev of the last n log returns ending at bar i (0.0 if too early)."""
    if i < n:
        return 0.0
    seg = np.asarray(c[i - n:i + 1], dtype=float)
    r = np.diff(np.log(np.clip(seg, 1e-9, None)))
    return float(np.std(r, ddof=1)) if len(r) > 1 else 0.0


def vol_regime(atr_arr, i: int, lookback: int = 500) -> str:
    """LOW / NORMAL / HIGH / EXTREME by ATR percentile over a TRAILING window
    (causal — never the full-sample distribution)."""
    if i < 60 or np.isnan(atr_arr[i]):
        return "UNKNOWN"
    lo = max(0, i - lookback)
    hist = atr_arr[lo:i]
    hist = hist[~np.isnan(hist)]
    if len(hist) < 30:
        return "UNKNOWN"
    p = float((hist < atr_arr[i]).mean())
    if p < 0.25:
        return "LOW"
    if p < 0.75:
        return "NORMAL"
    if p < 0.95:
        return "HIGH"
    return "EXTREME"


# ── session levels (prior day / opening range / initial balance) ─────────────

def prior_session_levels(bars: ds.Bars) -> dict[date, dict]:
    """Per session: high/low/mid/close/volume of the PRIOR RTH session, plus
    that session's value area. Keyed by the session they apply TO."""
    sessions = bars.rth_sessions()
    days = sorted(sessions)
    out = {}
    for k, d in enumerate(days):
        if k == 0:
            continue
        pd_ = days[k - 1]
        idxs = sessions[pd_]
        hi = float(max(bars.h[i] for i in idxs))
        lo = float(min(bars.l[i] for i in idxs))
        va = value_area([bars.c[i] for i in idxs], [bars.v[i] for i in idxs])
        out[d] = {"pdh": hi, "pdl": lo, "pdmid": (hi + lo) / 2.0,
                  "pdclose": float(bars.c[idxs[-1]]),
                  "pdvol": float(sum(bars.v[i] for i in idxs)),
                  "poc": va[0] if va else None,
                  "vah": va[1] if va else None,
                  "val": va[2] if va else None}
    return out


def opening_range(bars: ds.Bars, day: date, minutes: int = 30):
    """(high, low) of the first `minutes` of RTH. None if the session is short."""
    idxs = bars.rth_sessions().get(day, [])
    if not idxs:
        return None
    start = bars.minute_of_day(idxs[0])
    win = [i for i in idxs if bars.minute_of_day(i) < start + minutes]
    if not win:
        return None
    return float(max(bars.h[i] for i in win)), float(min(bars.l[i] for i in win))


def initial_balance(bars: ds.Bars, day: date):
    """Market-profile Initial Balance = first 60 minutes of RTH."""
    return opening_range(bars, day, minutes=60)


def session_vwap(bars: ds.Bars, idxs, upto_k: int) -> float | None:
    """Running session VWAP through bar idxs[upto_k] (inclusive), closes×volume
    — matches engine._session_vwap so research and live agree."""
    pv = vv = 0.0
    for k in range(upto_k + 1):
        i = idxs[k]
        pv += bars.c[i] * bars.v[i]
        vv += bars.v[i]
    return (pv / vv) if vv > 0 else None


def vwap_distance_atr(price: float, vwap: float | None, atr_val: float) -> float:
    """(price - vwap) / ATR — signed stretch from fair value, 0.0 if unusable."""
    if vwap is None or not atr_val or atr_val <= 0:
        return 0.0
    return (price - vwap) / atr_val


# ── volume / market profile ──────────────────────────────────────────────────

def value_area(closes, vols, pct: float = 0.70, bin_size: float = 1.0):
    """(POC, VAH, VAL) via 1-point volume bins, expanding from the POC by the
    larger adjacent bin until `pct` of volume is enclosed. None if empty."""
    hist = defaultdict(float)
    for c, v in zip(closes, vols):
        hist[round(float(c) / bin_size) * bin_size] += float(v)
    if not hist:
        return None
    prices = sorted(hist)
    total = sum(hist.values())
    if total <= 0:
        return None
    poc = max(prices, key=lambda p: hist[p])
    lo = hi = prices.index(poc)
    enc = hist[poc]
    while enc < pct * total and (lo > 0 or hi < len(prices) - 1):
        below = hist[prices[lo - 1]] if lo > 0 else -1.0
        above = hist[prices[hi + 1]] if hi < len(prices) - 1 else -1.0
        if above >= below:
            hi += 1
            enc += hist[prices[hi]]
        else:
            lo -= 1
            enc += hist[prices[lo]]
    return float(poc), float(prices[hi]), float(prices[lo])


def developing_value_area(bars: ds.Bars, idxs, upto_k: int, **kw):
    """Value area built only from bars up to and including idxs[upto_k]."""
    seg = idxs[:upto_k + 1]
    return value_area([bars.c[i] for i in seg], [bars.v[i] for i in seg], **kw)


# ── overnight inventory ──────────────────────────────────────────────────────

def overnight_features(bars: ds.Bars) -> dict[date, dict]:
    """Per session: overnight move (RTH open − prior RTH close), its trailing-60
    |move| percentile rank, and a top-tercile skew flag. All causal."""
    sessions = bars.rth_sessions()
    days = sorted(sessions)
    out, hist = {}, []
    for k, d in enumerate(days):
        if k == 0:
            continue
        prev_close = float(bars.c[sessions[days[k - 1]][-1]])
        open_px = float(bars.o[sessions[d][0]])
        move = open_px - prev_close
        rank = None
        if len(hist) >= 60:
            window = [abs(x) for x in hist[-60:]]
            rank = float((np.asarray(window) < abs(move)).mean())
        out[d] = {"on_move": move,
                  "on_abs_rank": rank,
                  "on_top_tercile": (rank is not None and rank >= 2 / 3),
                  "prev_rth_close": prev_close,
                  "rth_open": open_px}
        hist.append(move)
    return out


# ── dealer positioning (GEX) ─────────────────────────────────────────────────

def gex_regime_for_session(band_frac: float = 0.25, window: int = 250,
                           min_obs: int = 60) -> dict[date, dict]:
    """Session -> dealer-gamma regime from the daily SqueezeMetrics series.

    CAUSAL BY CONSTRUCTION: GEX known at the close of day t governs sessions
    AFTER t, and the neutral band uses only observations strictly before t.
    """
    series = sorted(ds.load_gex_daily().items())
    labelled, hist = [], []
    for d, g in series:
        if len(hist) >= min_obs:
            band = band_frac * statistics.median(abs(x) for x in hist[-window:])
            regime = "positive" if g > band else ("negative" if g < -band else "neutral")
        else:
            band, regime = 0.0, "neutral"
        labelled.append((d, {"gex": g, "band": band, "regime": regime}))
        hist.append(g)
    out = {}
    for k, (d, info) in enumerate(labelled):
        nxt = labelled[k + 1][0] if k + 1 < len(labelled) else date(2100, 1, 1)
        cur = d + timedelta(days=1)
        while cur <= nxt:
            out[cur] = info
            cur += timedelta(days=1)
    return out


# ── microstructure (from the nightly $0 GLBX MBO reduction) ─────────────────

def book_features(day: date, sym: str = "ES") -> dict | None:
    """1-second spread / mid / return series for a captured session.
    Returns None when that day wasn't captured."""
    bk = ds.load_book_1s(day, sym)
    if bk is None or len(bk["sec"]) == 0:
        return None
    bid = bk["bid"].astype(float) * PX_SCALE
    ask = bk["ask"].astype(float) * PX_SCALE
    mid = (bid + ask) / 2.0
    return {"sec": bk["sec"], "bid": bid, "ask": ask, "mid": mid,
            "spread": ask - bid,
            "spread_ticks": (ask - bid) / ds.SPECS[sym]["tick"]}


def iceberg_pressure(day: date, sym: str = "ES", min_fill: int = 25) -> dict:
    """Per-second signed hidden-liquidity pressure from per-order iceberg
    candidates: +1 per bid-side (defended floor) event, −1 per ask-side.
    {} when the session wasn't captured. (Round 23 showed this is scarce on
    MES and real on ES — hence the ES default.)"""
    events = ds.load_iceberg_events(day, sym)
    press = defaultdict(float)
    for _oid, rec in events.items():
        steps = rec.get("steps") or []
        if not steps:
            continue
        sec, cum = steps[-1]
        if cum < min_fill:
            continue
        press[int(sec)] += 1.0 if int(rec.get("side", 0)) == 0 else -1.0
    return dict(press)


# ── calendar ─────────────────────────────────────────────────────────────────

def session_phase(minute_of_day: int) -> str:
    if 9 * 60 + 30 <= minute_of_day <= 10 * 60:
        return "open"
    if 15 * 60 + 30 <= minute_of_day <= 16 * 60:
        return "close"
    if 10 * 60 < minute_of_day < 15 * 60 + 30:
        return "midday"
    return "overnight"


def calendar_features(bars: ds.Bars, i: int) -> dict:
    t = bars.ts[i]
    m = bars.minute_of_day(i)
    return {"minute_of_day": m, "dow": t.weekday(), "phase": session_phase(m)}


# ── assembled row ────────────────────────────────────────────────────────────

def feature_row(bars: ds.Bars, day: date, idxs, k: int, *,
                atr_arr, prior, overnight, gex) -> dict:
    """One causal feature row at bar idxs[k]. Everything is either computed
    from bars ≤ k or is a settled prior-session/prior-day value."""
    i = idxs[k]
    px = float(bars.c[i])
    a = float(atr_arr[i]) if not np.isnan(atr_arr[i]) else 0.0
    vwap = session_vwap(bars, idxs, k)
    p = prior.get(day, {})
    on = overnight.get(day, {})
    g = gex.get(day, {})
    row = {
        "price": px, "atr": a,
        "realized_vol": realized_vol(bars.c, i),
        "vol_regime": vol_regime(atr_arr, i),
        "vwap": vwap,
        "vwap_dist_atr": vwap_distance_atr(px, vwap, a),
        "pdh": p.get("pdh"), "pdl": p.get("pdl"), "pdmid": p.get("pdmid"),
        "poc": p.get("poc"), "vah": p.get("vah"), "val": p.get("val"),
        "on_move": on.get("on_move"), "on_abs_rank": on.get("on_abs_rank"),
        "on_top_tercile": on.get("on_top_tercile"),
        "gex": g.get("gex"), "gex_regime": g.get("regime"),
        "bars_into_session": k,
    }
    row.update(calendar_features(bars, i))
    if p.get("vah") is not None and a > 0:
        row["dist_to_vah_atr"] = (px - p["vah"]) / a
        row["dist_to_val_atr"] = (px - p["val"]) / a
    return row


def build_context(sym: str = "ES"):
    """Everything a round needs, computed once: bars, ATR, prior-session levels,
    overnight features, causal GEX regimes."""
    bars = ds.load_bars(sym)
    a = atr(bars.h, bars.l, bars.c)
    return {"bars": bars, "atr": a,
            "prior": prior_session_levels(bars),
            "overnight": overnight_features(bars),
            "gex": gex_regime_for_session()}
