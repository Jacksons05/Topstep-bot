"""Loaders for every data source this account already owns — all $0.

Sources (per the account holder's directive: use the FULL Databento + UW
subscription surface, but spend nothing further):

  Databento GLBX.MDP3 (owned outright, on disk)
    * 5-min OHLCV bars, ES/MES/MNQ, 2010 → 2026-06-06 (oos/data/*_5min.csv)
    * MES MBO months, Jan-2026 + May-2026 (Round 20 windows)
    * ES/MES 1-second top-of-book + iceberg-candidate artifacts from the
      nightly reduce-and-delete capture (oos/data/r23/*.npz|json)
  Databento GLBX plan entitlements (free to pull, quote-guarded elsewhere)
    * L1 trailing year, L2/L3 trailing month — used by oos/backfill_mbo.py
      and oos/round23_reduce.py, never re-downloaded here.
  SqueezeMetrics daily DIX/GEX (oos/data/squeeze_dix_gex.csv)
  Unusual Whales (live API, TTL-cached): options flow via uw_flow.py.

Everything returns plain numpy/dicts so rounds stay dependency-light and the
same arrays feed both backtests and the live engine.
"""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "oos" / "data"
R23 = DATA / "r23"
ET = ZoneInfo("America/New_York")

# Contract economics (CME): $/point, tick size, round-turn commission.
SPECS = {
    "ES":  {"pt": 50.0, "tick": 0.25, "comm_rt": 4.00},
    "MES": {"pt": 5.0,  "tick": 0.25, "comm_rt": 1.40},
    "MNQ": {"pt": 2.0,  "tick": 0.25, "comm_rt": 1.40},
    "NQ":  {"pt": 20.0, "tick": 0.25, "comm_rt": 4.00},
}

RTH_OPEN_MIN, RTH_CLOSE_MIN = 9 * 60 + 30, 16 * 60


class Bars:
    """5-min OHLCV series with ET timestamps and session indexing."""

    __slots__ = ("sym", "ts", "o", "h", "l", "c", "v", "_rth_days")

    def __init__(self, sym, ts, o, h, l, c, v):
        self.sym = sym
        self.ts, self.o, self.h, self.l, self.c, self.v = ts, o, h, l, c, v
        self._rth_days = None

    def __len__(self):
        return len(self.ts)

    @property
    def spec(self):
        return SPECS[self.sym]

    def minute_of_day(self, i) -> int:
        t = self.ts[i]
        return t.hour * 60 + t.minute

    def rth_sessions(self) -> dict[date, list[int]]:
        """date -> bar indices inside RTH (09:30–16:00 ET), weekdays only."""
        if self._rth_days is None:
            d = defaultdict(list)
            for i, t in enumerate(self.ts):
                if t.weekday() < 5 and RTH_OPEN_MIN <= self.minute_of_day(i) < RTH_CLOSE_MIN:
                    d[t.date()].append(i)
            self._rth_days = dict(d)
        return self._rth_days


@lru_cache(maxsize=8)
def load_bars(sym: str = "ES") -> Bars:
    """Databento 5-min bars from oos/data/{sym}_5min.csv (owned, $0)."""
    path = DATA / f"{sym}_5min.csv"
    ts, o, h, l, c, v = [], [], [], [], [], []
    with path.open() as f:
        for row in csv.DictReader(f):
            ts.append(datetime.fromisoformat(row["timestamp"]).astimezone(ET))
            o.append(float(row["open"]))
            h.append(float(row["high"]))
            l.append(float(row["low"]))
            c.append(float(row["close"]))
            v.append(float(row["volume"]))
    return Bars(sym, ts, np.array(o), np.array(h), np.array(l),
                np.array(c), np.array(v))


@lru_cache(maxsize=1)
def load_gex_daily() -> dict[date, float]:
    """SqueezeMetrics daily dealer net GEX (>0 = dealers long gamma).

    Returned keyed by the date the value is KNOWN (its own session close);
    callers must shift it forward themselves to stay causal — see
    features.gex_regime_for_session which does exactly that.
    """
    out = {}
    path = DATA / "squeeze_dix_gex.csv"
    if not path.exists():
        return out
    with path.open() as f:
        for row in csv.DictReader(f):
            try:
                out[date.fromisoformat(row["date"])] = float(row["gex"])
            except (ValueError, KeyError):
                continue
    return out


def load_book_1s(day: date, sym: str = "ES") -> dict | None:
    """1-second top-of-book from the nightly $0 GLBX MBO reduction.

    Returns {"sec": int64[], "bid": int64[], "ask": int64[]} in Databento
    fixed-precision price units (1e-9), or None when that session was never
    captured. Produced by oos/round23_reduce.py (reduce-and-delete: the raw
    multi-GB DBN is never retained).
    """
    p = R23 / f"{sym}_book1s_{day.isoformat()}.npz"
    if not p.exists():
        return None
    z = np.load(p)
    return {"sec": z["sec"], "bid": z["bid"], "ask": z["ask"]}


def load_iceberg_events(day: date, sym: str = "ES") -> dict:
    """Per-order iceberg candidates for a session (order_id -> {side, px,
    steps:[(sec, cum_fill)]}), from the same $0 nightly reduction. {} if absent."""
    p = R23 / f"{sym}_iceberg_{day.isoformat()}.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except ValueError:
        return {}


def captured_sessions(sym: str = "ES") -> list[date]:
    """Sessions available from the nightly reduction, ascending."""
    days = []
    for p in R23.glob(f"{sym}_book1s_*.npz"):
        try:
            days.append(date.fromisoformat(p.stem.split("_")[-1]))
        except ValueError:
            continue
    return sorted(days)


def inventory() -> dict:
    """What research data is on disk right now (for round write-ups)."""
    inv = {"bars": {}, "mbo_months": [], "captured_es": 0, "captured_mes": 0}
    for sym in ("ES", "MES", "MNQ"):
        p = DATA / f"{sym}_5min.csv"
        if p.exists():
            b = load_bars(sym)
            inv["bars"][sym] = {"rows": len(b),
                                "start": b.ts[0].date().isoformat(),
                                "end": b.ts[-1].date().isoformat()}
    inv["mbo_months"] = sorted(p.name for p in DATA.glob("*_mbo_*.dbn.zst"))
    inv["captured_es"] = len(captured_sessions("ES"))
    inv["captured_mes"] = len(captured_sessions("MES"))
    inv["gex_days"] = len(load_gex_daily())
    return inv


if __name__ == "__main__":
    print(json.dumps(inventory(), indent=1))
