"""Fetch multi-year CME futures 1-min bars from Databento, write 5-min CSVs.

Usage:
    .venv/bin/python oos/fetch_databento.py            # cost estimate only
    .venv/bin/python oos/fetch_databento.py --download # estimate, then download

Needs DATABENTO_API_KEY in Topstep-bot/.env (create at databento.com — signup
includes free credits that comfortably cover this pull).

Output: oos/data/{SYM}_5min.csv  (timestamp,open,high,low,close,volume — same
format topstep_futures_bt.py / backtest_oos.py consume).
"""
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "data"

load_dotenv(ROOT / ".env")

DATASET = "GLBX.MDP3"
SCHEMA = "ohlcv-1m"
# volume-rolled continuous front contract
JOBS = [
    ("ES", "ES.v.0", "2010-06-06"),
    ("MNQ", "MNQ.v.0", "2019-05-06"),
    ("MES", "MES.v.0", "2019-05-06"),
]
END = "2026-06-06"  # everything after this is the in-sample window — excluded

MAX_AUTO_COST_USD = 45.0  # verified estimate 2026-07-03: $38.64, covered by signup credits


def main() -> int:
    key = os.environ.get("DATABENTO_API_KEY", "")
    if not key:
        print("DATABENTO_API_KEY missing from .env — create a key at "
              "https://databento.com (free signup credits cover this pull), "
              "add the line DATABENTO_API_KEY=... to Topstep-bot/.env, rerun.",
              file=sys.stderr)
        return 1

    import databento as db
    client = db.Historical(key)

    total_cost = 0.0
    for sym, cont, start in JOBS:
        cost = client.metadata.get_cost(
            dataset=DATASET, symbols=[cont], stype_in="continuous",
            schema=SCHEMA, start=start, end=END,
        )
        total_cost += cost
        print(f"{sym} ({cont}) {start}..{END}: ${cost:.2f}")
    print(f"TOTAL estimated cost: ${total_cost:.2f}")

    if "--download" not in sys.argv:
        print("Estimate only. Rerun with --download to fetch.")
        return 0
    if total_cost > MAX_AUTO_COST_USD:
        print(f"ABORT: estimate ${total_cost:.2f} > ${MAX_AUTO_COST_USD:.2f} "
              "safety cap. Raise MAX_AUTO_COST_USD only after checking the "
              "symbols are right.", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    for sym, cont, start in JOBS:
        print(f"downloading {sym} ...")
        data = client.timeseries.get_range(
            dataset=DATASET, symbols=[cont], stype_in="continuous",
            schema=SCHEMA, start=start, end=END,
        )
        df = data.to_df()
        if df.empty:
            print(f"{sym}: EMPTY response", file=sys.stderr)
            continue
        # prices are floats in the client df; index = ts_event (UTC)
        bars5 = (
            df[["open", "high", "low", "close", "volume"]]
            .resample("5min")
            .agg({"open": "first", "high": "max", "low": "min",
                  "close": "last", "volume": "sum"})
            .dropna(subset=["close"])
        )
        out = OUT / f"{sym}_5min.csv"
        bars5.to_csv(out, index_label="timestamp")
        print(f"{sym}: {len(df)} 1-min -> {len(bars5)} 5-min bars "
              f"{bars5.index[0]} .. {bars5.index[-1]} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
