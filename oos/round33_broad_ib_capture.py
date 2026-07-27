"""Round 33 (oos/HYPOTHESES.md): broad-universe opening-balance break, FORWARD
CAPTURE ONLY. No backtest, no P&L judgment here -- this script's entire job is
to append one clean row of observed facts per (instrument, window) to a local
CSV, once a day, after the 16:08 ET flatten. The analysis/judgment script gets
written once >=40 trading days exist (see the PASS bar in HYPOTHESES.md).

Registered with a DEAD prior: the identical mechanism on ES (Screen,
2026-07-20) was a coin flip (continuation base rate 0.504) and both
continuation and its mirror fade lost money. This extends the same test to the
other 27 Topstep-tradeable products to see whether that holds outside equity
index futures -- not because there's a fresh reason to expect a positive
result.

Zero API cost: everything here comes off the ProjectX connection the Topstep
account already pays for (contract search + historical bars), nothing paid,
no Databento. Runs locally via Windows Task Scheduler, once per trading day,
any time after 16:08 ET (the account-wide flatten) so the full day's bars are
already final. Intended to run standalone, NOT through engine.py / run.py --
it never builds an Engine, never touches state.py or the risk layer, and
places no orders; it only reads.

Usage:
    .venv\\Scripts\\python oos\\round33_broad_ib_capture.py
"""
from __future__ import annotations

import csv
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import config  # noqa: E402
config.load_dotenv()
from config import CONFIG  # noqa: E402
from projectx_executor import ProjectXBroker  # noqa: E402

_ET = ZoneInfo("America/New_York")
CSV_PATH = HERE / "round33_capture.csv"

# The 28 instruments confirmed live via /api/Contract/search on 2026-07-27
# (see the Round 33 registration for the full rationale). Grouped only for
# readability -- the capture treats every symbol identically.
UNIVERSE = [
    # equity indices
    "ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K",
    # energy
    "CL", "MCL", "NG", "MNG",
    # metals
    "GC", "MGC", "SI", "HG", "MHG", "PL",
    # rates
    "ZB", "ZN",
    # currencies
    "6E", "M6E", "6B", "M6B", "6J", "6A", "M6A", "6C",
    # grains
    "ZC", "ZS", "ZW",
    # livestock
    "HE", "LE",
]

CSV_FIELDS = [
    "date", "symbol", "window_min", "session_open_et", "tick_size", "tick_value",
    "ibh", "ibl", "break_side", "break_time_et", "break_price",
    "continuation_exit_price", "fade_exit_price", "notes",
]


def _bars_et(broker: ProjectXBroker, symbol: str, limit: int = 500) -> list[dict]:
    """Today's bars (plus some overnight lookback), parsed into ET-stamped
    dicts sorted oldest->newest. Empty list on any failure -- callers must
    treat that as 'skip this symbol today', not raise."""
    raw = broker.historical_bars(symbol, timeframe=CONFIG.scalp_timeframe, limit=limit)
    closes = raw.get("close") or []
    if not closes:
        return []
    out = []
    for i in range(len(closes)):
        try:
            ts = datetime.fromisoformat(raw["time"][i]).astimezone(_ET)
        except (ValueError, TypeError, IndexError, KeyError):
            continue
        out.append({
            "ts": ts, "open": raw["open"][i], "high": raw["high"][i],
            "low": raw["low"][i], "close": raw["close"][i], "volume": raw["volume"][i],
        })
    return out


def _todays_bars(bars: list[dict], today: date) -> list[dict]:
    return [b for b in bars if b["ts"].date() == today]


def detect_session_open(bars: list[dict]) -> datetime | None:
    """Empirically find where today's bar-volume steps up from its quiet
    baseline, rather than trusting a hardcoded RTH time table (deliberately
    NOT sourced from an external site -- see Round 33's registration for why).

    Heuristic: the first bar whose own volume exceeds 3x the day's median bar
    volume, confirmed by the following bar also staying above the median (so
    a single outlier print isn't mistaken for the open). Returns THAT bar's
    own timestamp -- not an earlier bar in some lookback window -- since the
    step happens AT this bar, not before it. None if the day's volume never
    shows a clear step (illiquid product / holiday-thin session -- caller
    must skip, not guess)."""
    if len(bars) < 4:
        return None
    vols = [b["volume"] for b in bars]
    med = sorted(vols)[len(vols) // 2]
    if med <= 0:
        return None
    for i in range(len(bars) - 1):
        if vols[i] > 3.0 * med and vols[i + 1] > med:
            return bars[i]["ts"]
    return None


def initial_balance(bars: list[dict], session_open: datetime, window_min: int):
    """(ibh, ibl, window_end_ts) from bars in [session_open, session_open +
    window_min). None if no bars fall in that window."""
    window_end = session_open + timedelta(minutes=window_min)
    seg = [b for b in bars if session_open <= b["ts"] < window_end]
    if not seg:
        return None
    return max(b["high"] for b in seg), min(b["low"] for b in seg), window_end


def first_break(bars: list[dict], window_end: datetime, ibh: float, ibl: float):
    """(side, break_time, break_price, next_bar_open) for the FIRST bar after
    window_end whose close breaks IBH (side='up') or IBL (side='down').
    'next_bar_open' is the entry price (next-bar-open, no lookahead) -- None
    if there is no bar after the break bar yet (today's break hasn't been
    followed by another print), which just means no entry price is available
    today; the break itself is still recorded."""
    seg = [b for b in bars if b["ts"] >= window_end]
    for i, b in enumerate(seg):
        side = "up" if b["close"] > ibh else "down" if b["close"] < ibl else None
        if side is None:
            continue
        entry = seg[i + 1]["open"] if i + 1 < len(seg) else None
        return side, b["ts"], b["close"], entry
    return None, None, None, None


def capture_symbol(broker: ProjectXBroker, symbol: str, today: date) -> list[dict]:
    bars_all = _bars_et(broker, symbol)
    if not bars_all:
        return [{"date": today, "symbol": symbol, "window_min": w, "notes": "no bars"}
                for w in (30, 60)]
    bars_today = _todays_bars(bars_all, today)
    session_open = detect_session_open(bars_today)
    if session_open is None:
        return [{"date": today, "symbol": symbol, "window_min": w,
                  "notes": "no session-open detected"} for w in (30, 60)]

    # Pull tick specs from the SAME live contract-search call used for order
    # routing elsewhere -- not hand-typed, so it can't silently drift from
    # what the account actually trades.
    tick_size = tick_value = None
    try:
        cid = broker.contract_id(symbol)
        spec = broker._post("/api/Contract/search", {"searchText": symbol, "live": broker._live})
        for c in spec.get("contracts") or []:
            if c.get("id") == cid:
                tick_size, tick_value = c.get("tickSize"), c.get("tickValue")
                break
    except Exception:  # noqa: BLE001 - cost data is a nice-to-have, never blocks the capture row
        pass

    flatten_cutoff = datetime.combine(today, datetime.min.time(), tzinfo=_ET).replace(hour=16, minute=8)
    rows = []
    for window_min in (30, 60):
        ib = initial_balance(bars_today, session_open, window_min)
        if ib is None:
            rows.append({"date": today, "symbol": symbol, "window_min": window_min,
                         "session_open_et": session_open.strftime("%H:%M"),
                         "notes": "no bars in IB window"})
            continue
        ibh, ibl, window_end = ib
        side, btime, bprice, entry = first_break(bars_today, window_end, ibh, ibl)
        row = {
            "date": today, "symbol": symbol, "window_min": window_min,
            "session_open_et": session_open.strftime("%H:%M"),
            "tick_size": tick_size, "tick_value": tick_value,
            "ibh": ibh, "ibl": ibl,
            "break_side": side, "break_time_et": btime.strftime("%H:%M") if btime else None,
            "break_price": bprice, "notes": "",
        }
        if side is not None and entry is not None:
            at_flatten = [b for b in bars_today if b["ts"] <= flatten_cutoff]
            exit_px = at_flatten[-1]["close"] if at_flatten else None
            if exit_px is not None:
                cont_pnl_pts = (exit_px - entry) if side == "up" else (entry - exit_px)
                row["continuation_exit_price"] = exit_px
                row["fade_exit_price"] = exit_px  # same exit price; sign flips in analysis
                row["notes"] = f"entry={entry} cont_pts={cont_pnl_pts:+.4f}"
            else:
                row["notes"] = "break seen, no bar at/after flatten yet"
        elif side is not None:
            row["notes"] = "break seen, no next-bar-open available yet"
        rows.append(row)
    return rows


def append_rows(rows: list[dict]) -> None:
    is_new = not CSV_PATH.exists()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if is_new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    if not (CONFIG.projectx_username and CONFIG.projectx_api_key):
        print("PROJECTX_USERNAME/PROJECTX_API_KEY not set -- nothing to capture. "
              "Fill these in .env yourself (never share them in chat).")
        return 1
    broker = ProjectXBroker()
    if getattr(broker, "_mock_mode", True):
        print("Broker came up in mock mode (bad/missing credentials?) -- aborting, "
              "not writing placeholder rows.")
        return 1

    today = datetime.now(_ET).date()
    all_rows = []
    for sym in UNIVERSE:
        try:
            all_rows.extend(capture_symbol(broker, sym, today))
        except Exception as exc:  # noqa: BLE001 - one bad symbol must not lose the rest
            all_rows.append({"date": today, "symbol": sym, "window_min": "",
                             "notes": f"capture error: {exc}"})
    append_rows(all_rows)
    n_ok = sum(1 for r in all_rows if not r.get("notes", "").startswith(("no ", "capture error")))
    print(f"Round 33 capture {today}: {len(all_rows)} rows written to {CSV_PATH} "
          f"({n_ok} usable, {len(all_rows) - n_ok} skipped/errored).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
