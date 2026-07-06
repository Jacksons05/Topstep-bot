"""Reduce 1 month of ES mbp-10 ticks to 1-second features for Round 5.

Streams the DBN file in chunks (never loads it whole). Emits one row per
second: last bid/ask (L1, price units), last 10-level OBI, and the signed
aggressor trade volume in that second.

Output: oos/data/ES_of_1s.npz
Usage:  .venv/bin/python oos/mbp10_features.py
"""
from pathlib import Path

import numpy as np
import databento as db

HERE = Path(__file__).resolve().parent
SRC = HERE / "data" / "ES_mbp10_1mo.dbn.zst"
OUT = HERE / "data" / "ES_of_1s.npz"
CHUNK = 2_000_000
PX = 1e-9  # Databento fixed-precision scale


def main() -> int:
    store = db.DBNStore.from_file(SRC)
    start_ns = None
    # accumulators sized on first chunk (grow-safe: 40 days of seconds)
    n_sec = 40 * 86400
    last_obi = np.full(n_sec, np.nan)
    last_bid = np.full(n_sec, np.nan)
    last_ask = np.full(n_sec, np.nan)
    tvol = np.zeros(n_sec)
    seen = np.zeros(n_sec, dtype=bool)

    bid_cols = [f"bid_sz_{i:02d}" for i in range(10)]
    ask_cols = [f"ask_sz_{i:02d}" for i in range(10)]

    total = 0
    for arr in store.to_ndarray(count=CHUNK):
        ts = arr["ts_event"].astype(np.int64)
        if start_ns is None:
            start_ns = int(ts[0]) - int(ts[0]) % 1_000_000_000
        sec = ((ts - start_ns) // 1_000_000_000).astype(np.int64)
        sec = np.clip(sec, 0, n_sec - 1)

        bid_sz = np.sum([arr[c].astype(np.float64) for c in bid_cols], axis=0)
        ask_sz = np.sum([arr[c].astype(np.float64) for c in ask_cols], axis=0)
        denom = bid_sz + ask_sz
        obi = np.where(denom > 0, (bid_sz - ask_sz) / denom, 0.0)
        bid_px = arr["bid_px_00"].astype(np.float64) * PX
        ask_px = arr["ask_px_00"].astype(np.float64) * PX

        # last record per second wins (records are time-ordered)
        last_obi[sec] = obi
        last_bid[sec] = bid_px
        last_ask[sec] = ask_px
        seen[sec] = True

        # signed aggressor volume: side B = buy aggressor, A = sell aggressor
        is_trade = arr["action"] == b"T"
        if is_trade.any():
            t_sec = sec[is_trade]
            sign = np.where(arr["side"][is_trade] == b"B", 1.0, -1.0)
            np.add.at(tvol, t_sec, sign * arr["size"][is_trade].astype(np.float64))

        total += len(arr)
        if total % 20_000_000 < CHUNK:
            print(f"processed {total:,} records", flush=True)

    idx = np.flatnonzero(seen)
    np.savez_compressed(
        OUT,
        sec=idx, start_ns=np.int64(start_ns),
        obi=last_obi[idx], bid=last_bid[idx], ask=last_ask[idx], tvol=tvol[idx],
    )
    print(f"done: {total:,} records -> {len(idx):,} seconds -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
