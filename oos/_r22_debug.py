"""Round 22 detector funnel diagnostics (no thresholds changed — measurement
only). For each RTH scan-second, record the best candidate level's stats so
we can see which frozen condition binds: alive-age, fill-event count, volume.
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import round22_absorption as r22  # noqa: E402
from round22_absorption import (Book, _day_bounds, BID, ASK, TICK_I,  # noqa: E402
                                ABS_WINDOW_S, ABS_MAX_TICKS_FROM_TOUCH,
                                GAP_RESET_S, SIGMA_WINDOW, CHUNK)
import databento as db  # noqa: E402

MAX_MSGS = 40_000_000
path = r22.WINDOWS["W1_2026-01"]

bk = Book()
store = db.DBNStore.from_file(path)
cur_sec = -1
day_roll = -1
e_first = e_last = flat_sec = 0
mid_n = 0
scanned = 0
best_events = []   # per scanned second: max events among candidates
best_vol = []
best_age = []
alive_candidates = 0

def scan(sec):
    """Duty-cycle only (amendment 79680f8): does ANY candidate level pass the
    amended detector this second? No trades, no P&L."""
    global scanned, alive_candidates
    bb, ba = bk.best
    if not (bb and ba and ba > bb):
        return
    scanned += 1
    hit = 0
    for s, touch, step in ((BID, bb, -TICK_I), (ASK, ba, TICK_I)):
        for k in range(ABS_MAX_TICKS_FROM_TOUCH + 1):
            px = touch + step * k
            if bk.absorption_at(s, px, sec):
                hit = 1
                break
        if hit:
            break
    best_events.append(hit)

n_msgs = 0
for arr in store.to_ndarray(count=CHUNK):
    ts = arr["ts_event"].astype(np.int64)
    act = arr["action"]
    side_c = arr["side"]
    px_a = arr["price"].astype(np.int64)
    sz_a = arr["size"].astype(np.int64)
    oid_a = arr["order_id"].astype(np.int64)
    for i in range(len(ts)):
        sec = int(ts[i]) // 1_000_000_000
        bk.msg_sec = sec
        if cur_sec == -1:
            cur_sec = sec
        elif sec > cur_sec:
            if sec - cur_sec > GAP_RESET_S:
                cur_sec = sec
            while cur_sec < sec:
                s0 = cur_sec
                cur_sec += 1
                if s0 >= day_roll:
                    e_first, e_last, flat_sec, day_roll = _day_bounds(s0)
                if e_first <= s0 <= e_last:
                    scan(s0)
        a = act[i]
        oid = int(oid_a[i])
        if a == b"A":
            s = BID if side_c[i] == b"B" else (ASK if side_c[i] == b"A" else -1)
            if s >= 0:
                bk.add(oid, int(px_a[i]), int(sz_a[i]), s)
        elif a == b"C":
            bk.remove(oid)
        elif a == b"M":
            if oid in bk.orders:
                bk.remove(oid)
                s = BID if side_c[i] == b"B" else (ASK if side_c[i] == b"A" else -1)
                if s >= 0:
                    bk.add(oid, int(px_a[i]), int(sz_a[i]), s)
        elif a == b"F":
            bk.remove(oid, qty=int(sz_a[i]), record_fill=True)
        elif a == b"R":
            bk.clear()
    n_msgs += len(ts)
    if n_msgs >= MAX_MSGS:
        break

be = np.array(best_events)
print(f"msgs={n_msgs/1e6:.0f}M scanned_secs={scanned}")
if scanned:
    print(f"  amended-detector duty cycle: {be.mean():.4%} of RTH seconds "
          f"({int(be.sum())} hit-seconds; UNTESTABLE threshold is 5%)")
