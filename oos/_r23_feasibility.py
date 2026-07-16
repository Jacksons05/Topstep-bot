"""P&L-blind feasibility probe: are per-ORDER iceberg refills visible in the
MES MBO feed? Counts orders whose cumulative filled volume exceeds k× their
maximum displayed size (the literature iceberg signature). No prices used
beyond grouping, no direction, no outcomes — detector-object feasibility only.
"""
from __future__ import annotations

import numpy as np
import databento as db
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
MAX_MSGS = 40_000_000
CHUNK = 4_000_000

store = db.DBNStore.from_file(DATA / "MES_mbo_2026-01-06_2026-02-06.dbn.zst")

# order_id -> [max_displayed, cum_filled, n_fills, n_size_upticks]
orders: dict[int, list] = {}
cur_size: dict[int, int] = {}

n = 0
for arr in store.to_ndarray(count=CHUNK):
    act = arr["action"]
    sz_a = arr["size"].astype(np.int64)
    oid_a = arr["order_id"].astype(np.int64)
    for i in range(len(act)):
        a = act[i]
        oid = int(oid_a[i])
        sz = int(sz_a[i])
        if a == b"A":
            cur_size[oid] = sz
            orders[oid] = [sz, 0, 0, 0]
        elif a == b"M":
            rec = orders.get(oid)
            if rec is not None:
                old = cur_size.get(oid, 0)
                if sz > old:
                    rec[3] += 1                  # size uptick = refill candidate
                rec[0] = max(rec[0], sz)
                cur_size[oid] = sz
        elif a == b"F":
            rec = orders.get(oid)
            if rec is not None:
                rec[1] += sz
                rec[2] += 1
                cur_size[oid] = max(cur_size.get(oid, 0) - sz, 0)
        elif a == b"C":
            cur_size.pop(oid, None)
        elif a == b"R":
            cur_size.clear()
    n += len(act)
    if n >= MAX_MSGS:
        break

stats = [(r[1], r[0], r[2], r[3]) for r in orders.values() if r[1] > 0]
print(f"msgs={n/1e6:.0f}M | orders seen={len(orders)/1e6:.1f}M | filled>0: {len(stats)}")
for k in (1.5, 2.0, 3.0):
    hits = [s for s in stats if s[0] >= k * max(s[1], 1) and s[0] >= 20 and s[3] >= 1]
    print(f"  iceberg candidates (cum_fill >= {k}x max_disp, >=20 filled, "
          f">=1 refill uptick): {len(hits)}")
hits = [s for s in stats if s[0] >= 2.0 * max(s[1], 1) and s[0] >= 20 and s[3] >= 1]
if hits:
    fv = np.array([h[0] for h in hits])
    print(f"  @2x: filled-volume p50={np.percentile(fv,50):.0f} "
          f"p90={np.percentile(fv,90):.0f} max={fv.max()}; "
          f"per-session ≈ {len(hits)/3:.0f}")
