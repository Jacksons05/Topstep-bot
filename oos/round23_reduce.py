"""Round 23 Phase-0 reducer: per-day ES MBO → compact artifacts, raw deleted.

For each session in the registered window (2026-06-17 → 2026-07-15):
  1. Quote the day's ES.v.0 MBO cost — ANY nonzero quote skips the day and
     logs it (the account holder authorized zero further Databento spend).
  2. Stream the day once, building the per-order book, and emit:
       oos/data/r23/ES_book1s_<day>.npz   — 1 s best bid/ask (int64 price_i)
       oos/data/r23/ES_iceberg_<day>.json — candidate event log: for every
         order that EVER satisfies (refill-uptick ≥ 1, cum_fill ≥ 2.0× max
         displayed, cum_fill ≥ 25), each subsequent (sec, cum_fill) step with
         the order's side and price — enough for Phase B to reconstruct the
         first-crossing signal time for ANY threshold ≥ 25 without re-reading
         raw data (registration 18f21ac).
  3. DELETE the raw .dbn.zst before the next day (host disk is the binding
     constraint: ~93 GB free on C:, and the WSL VHDX grows into it).

Idempotent per day (existing artifacts skip). Usage:
    .venv/bin/python oos/round23_reduce.py [--start YYYY-MM-DD] [--end YYYY-MM-DD]
"""
from __future__ import annotations

import json
import os
import sys
import time as _time
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
import databento as db  # noqa: E402

OUT = HERE / "data" / "r23"
RAW = HERE / "data" / "r23" / "_raw"
START = date(2026, 6, 17)
END = date(2026, 7, 15)          # inclusive
SYMBOL = "ES.v.0"
CHUNK = 4_000_000
MAX_DAY_COST = 0.0               # zero-spend directive: anything above skips
REFILL_RATIO = 2.0
MIN_FILL = 25

BID, ASK = 0, 1


def reduce_day(client: db.Historical, d: date) -> str:
    book_path = OUT / f"ES_book1s_{d.isoformat()}.npz"
    ice_path = OUT / f"ES_iceberg_{d.isoformat()}.json"
    if book_path.exists() and ice_path.exists():
        return "cached"
    params = dict(dataset="GLBX.MDP3", schema="mbo", symbols=[SYMBOL],
                  stype_in="continuous",
                  start=f"{d.isoformat()}T00:00",
                  end=f"{(d + timedelta(days=1)).isoformat()}T00:00")
    cost = client.metadata.get_cost(**params)
    if cost > MAX_DAY_COST:
        return f"SKIPPED (quoted ${cost:.2f} > $0 — zero-spend directive)"
    RAW.mkdir(parents=True, exist_ok=True)
    raw = RAW / f"ES_{d.isoformat()}.dbn.zst"
    try:
        if not raw.exists():
            client.timeseries.get_range(**params, path=str(raw))

        store = db.DBNStore.from_file(raw)
        # per-order: [side, price_i, max_disp, cum_fill, refills, cur_size]
        orders: dict[int, list] = {}
        # candidates: oid -> {"side","px","steps":[(sec,cum_fill),...]}
        cands: dict[int, dict] = {}
        secs: list[int] = []
        bids: list[int] = []
        asks: list[int] = []
        lv = ({}, {})
        best = [0, 0]
        cur_sec = -1

        def sample_to(target):
            nonlocal cur_sec
            if target - cur_sec > 3600:
                cur_sec = target
                return
            while cur_sec < target:
                s0 = cur_sec
                cur_sec += 1
                if best[BID] and best[ASK] and best[ASK] > best[BID]:
                    secs.append(s0)
                    bids.append(best[BID])
                    asks.append(best[ASK])

        def _add(oid, px, sz, s):
            orders[oid] = [s, px, sz, 0, 0, sz]
            lvs = lv[s]
            lvs[px] = lvs.get(px, 0) + sz
            if s == BID:
                if px > best[BID]:
                    best[BID] = px
            elif best[ASK] == 0 or px < best[ASK]:
                best[ASK] = px

        def _sub_level(s, px, take):
            lvs = lv[s]
            rem = lvs.get(px, 0) - take
            if rem > 0:
                lvs[px] = rem
            else:
                lvs.pop(px, None)
                if px == best[s]:
                    best[s] = (max(lvs) if s == BID else min(lvs)) if lvs else 0

        n_msgs = 0
        t0 = _time.time()
        for arr in store.to_ndarray(count=CHUNK):
            ts = arr["ts_event"].astype(np.int64)
            act = arr["action"]
            side_c = arr["side"]
            px_a = arr["price"].astype(np.int64)
            sz_a = arr["size"].astype(np.int64)
            oid_a = arr["order_id"].astype(np.int64)
            for i in range(len(ts)):
                sec = int(ts[i]) // 1_000_000_000
                if cur_sec == -1:
                    cur_sec = sec
                elif sec > cur_sec:
                    sample_to(sec)
                a = act[i]
                oid = int(oid_a[i])
                if a == b"A":
                    s = BID if side_c[i] == b"B" else (ASK if side_c[i] == b"A" else -1)
                    if s >= 0:
                        _add(oid, int(px_a[i]), int(sz_a[i]), s)
                elif a == b"C":
                    rec = orders.pop(oid, None)
                    if rec is not None:
                        _sub_level(rec[0], rec[1], rec[5])
                elif a == b"M":
                    rec = orders.get(oid)
                    if rec is not None:
                        s, px, sz = rec[0], int(px_a[i]), int(sz_a[i])
                        if sz > rec[5]:
                            rec[4] += 1              # refill uptick
                        _sub_level(s, rec[1], rec[5])
                        ns = BID if side_c[i] == b"B" else (ASK if side_c[i] == b"A" else s)
                        rec[0], rec[1], rec[5] = ns, px, sz
                        rec[2] = max(rec[2], sz)
                        lvs = lv[ns]
                        lvs[px] = lvs.get(px, 0) + sz
                        if ns == BID:
                            if px > best[BID]:
                                best[BID] = px
                        elif best[ASK] == 0 or px < best[ASK]:
                            best[ASK] = px
                elif a == b"F":
                    rec = orders.get(oid)
                    if rec is not None:
                        take = min(int(sz_a[i]), rec[5])
                        rec[3] += int(sz_a[i])
                        rec[5] -= take
                        _sub_level(rec[0], rec[1], take)
                        # NOTE: do NOT pop the record at cur_size==0 — display
                        # exhaustion is exactly when an iceberg's refill-modify
                        # arrives, and popping here made refills invisible
                        # (first reducer version found 0 candidates because of
                        # this). The record survives until an explicit Cancel.
                        # candidate bookkeeping (registration thresholds)
                        if (rec[4] >= 1 and rec[3] >= MIN_FILL
                                and rec[3] >= REFILL_RATIO * max(rec[2], 1)):
                            c = cands.get(oid)
                            if c is None:
                                c = {"side": rec[0], "px": rec[1], "steps": []}
                                cands[oid] = c
                            c["steps"].append((sec, rec[3]))
                elif a == b"R":
                    orders.clear()
                    lv[BID].clear()
                    lv[ASK].clear()
                    best[BID] = best[ASK] = 0
            n_msgs += len(ts)

        OUT.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(book_path, sec=np.array(secs, dtype=np.int64),
                            bid=np.array(bids, dtype=np.int64),
                            ask=np.array(asks, dtype=np.int64))
        ice_path.write_text(json.dumps(
            {str(k): v for k, v in cands.items()}))
        rate = n_msgs / max(_time.time() - t0, 1e-9)
        return (f"ok ({n_msgs/1e6:.0f}M msgs @ {rate/1e6:.2f}M/s, "
                f"{len(cands)} candidates)")
    finally:
        raw.unlink(missing_ok=True)      # host disk is the binding constraint


def main() -> int:
    args = sys.argv[1:]
    start = date.fromisoformat(args[args.index("--start") + 1]) if "--start" in args else START
    end = date.fromisoformat(args[args.index("--end") + 1]) if "--end" in args else END
    client = db.Historical(os.environ["DATABENTO_API_KEY"])
    d = start
    while d <= end:
        if d.isoweekday() != 6:          # skip UTC Saturdays (no session)
            print(f"{d}: ", end="", flush=True)
            try:
                print(reduce_day(client, d), flush=True)
            except Exception as e:  # noqa: BLE001 — one bad day must not kill the run
                print(f"ERROR {e}", flush=True)
        d += timedelta(days=1)
    print("REDUCE COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
