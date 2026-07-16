"""Round 20 data pull — MES MBO, two non-adjacent one-month windows.

See oos/HYPOTHESES.md "Round 20" for the frozen spec. Cost confirmed via
metadata.get_cost() 2026-07-16: $55.43 total (MES only, both windows).

Usage:
    .venv/bin/python oos/fetch_round20_mbo.py            # cost estimate only
    .venv/bin/python oos/fetch_round20_mbo.py --download # estimate, then download

Output: oos/data/MES_mbo_{window}.dbn.zst (Databento Binary Encoding, native
format — oos/round20_maker_orderflow.py will read these directly via
databento.DBNStore, no conversion needed for order-level queue simulation).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "data"

load_dotenv(ROOT / ".env")

import databento as db

DATASET = "GLBX.MDP3"
SCHEMA = "mbo"
SYMBOL = "MES.v.0"
WINDOWS = [
    ("2026-01-06", "2026-02-06"),
    ("2026-05-06", "2026-06-06"),
]
MAX_AUTO_COST_USD = 65.0  # confirmed estimate 2026-07-16: $55.43


def main() -> int:
    key = os.environ.get("DATABENTO_API_KEY", "")
    if not key:
        print("DATABENTO_API_KEY missing from .env", file=sys.stderr)
        return 1

    client = db.Historical(key)

    total_cost = 0.0
    for start, end in WINDOWS:
        cost = client.metadata.get_cost(
            dataset=DATASET, symbols=[SYMBOL], stype_in="continuous",
            schema=SCHEMA, start=start, end=end,
        )
        total_cost += cost
        print(f"MES {start}..{end}: ${cost:.2f}")
    print(f"TOTAL estimated cost: ${total_cost:.2f}")

    if "--download" not in sys.argv:
        print("Estimate only. Rerun with --download to fetch.")
        return 0
    if total_cost > MAX_AUTO_COST_USD:
        print(f"ABORT: estimate ${total_cost:.2f} > ${MAX_AUTO_COST_USD:.2f} "
              "safety cap.", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    for start, end in WINDOWS:
        out_path = OUT / f"MES_mbo_{start}_{end}.dbn.zst"
        print(f"downloading MES {start}..{end} -> {out_path} ...")
        store = client.timeseries.get_range(
            dataset=DATASET, symbols=[SYMBOL], stype_in="continuous",
            schema=SCHEMA, start=start, end=end,
            path=out_path,
        )
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"  done: {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
