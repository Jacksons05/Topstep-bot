"""Round 22 — hidden-liquidity absorption (iceberg defense) on MES MBO.

Implements exactly the registered spec (HYPOTHESES.md Round 22, commit
2abecea, frozen BEFORE this file was written). Chassis shared with Round 20
(chunked MBO streaming, per-order book, 1 s grid, 30-min mid-σ), detector
replaced: a level within 4 ticks of the touch that absorbs ≥ 100 contracts
across ≥ 5 fills in 120 s while its displayed size never touches zero.

Taker entry at the next 1 s snapshot; bracket 1.0σ/1.5σ (ticks-snapped,
stop-first); 15-min max hold; RTH entries 09:35–15:30 ET, flatten 15:55;
level re-arm cooldown 10 min. Costs $1.40 RT + 1 tick per side.

Usage:
    .venv/bin/python oos/round22_absorption.py            # both windows
    .venv/bin/python oos/round22_absorption.py --smoke    # first 20M msgs W1
"""
from __future__ import annotations

import json
import sys
import time as _time
from collections import deque
from datetime import datetime, timedelta
from math import erf, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import databento as db

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ET = ZoneInfo("America/New_York")

WINDOWS = {
    "W1_2026-01": DATA / "MES_mbo_2026-01-06_2026-02-06.dbn.zst",
    "W2_2026-05": DATA / "MES_mbo_2026-05-06_2026-06-06.dbn.zst",
}

PX = 1e-9
TICK_I = 250_000_000          # 0.25 in 1e-9 units
PT_USD = 5.0
COMM_RT = 1.40
SLIP_USD = 2 * 0.25 * PT_USD  # 1 tick per side, taker both ways = $2.50

# ── frozen detector (registration 2abecea + amendment 79680f8) ──────────────
ABS_WINDOW_S = 120
ABS_MIN_VOL = 300             # amendment (a'): above measured p90 of churn
ABS_MIN_EVENTS = 5
ABS_REFILL_RATIO = 3.0        # amendment (b'): consumed >= 3x max displayed
ABS_MAX_TICKS_FROM_TOUCH = 4
REARM_S = 600
# ── frozen trade rules ────────────────────────────────────────────────────────
SIGMA_WINDOW = 1800           # 30-min of 1 s mids
STOP_SIG = 1.0
TGT_SIG = 1.5
MAX_HOLD_S = 900
ENTRY_FIRST = (9, 35)
ENTRY_LAST = (15, 30)
FLATTEN = (15, 55)
GAP_RESET_S = 120
CHUNK = 4_000_000
BOOT_N = 20_000
RNG_SEED = 7

BID, ASK = 0, 1


class Book:
    def __init__(self):
        self.orders: dict[int, tuple[int, int, int]] = {}
        self.levels = ({}, {})
        self.best = [0, 0]
        self.alive_since: dict[tuple[int, int], int] = {}   # (side,px) -> sec of last 0->+
        self.fills: dict[tuple[int, int], deque] = {}       # (side,px) -> deque[(sec,size)]
        self.msg_sec = 0

    def add(self, oid, px, sz, s):
        self.orders[oid] = (px, sz, s)
        lv = self.levels[s]
        prev = lv.get(px, 0)
        lv[px] = prev + sz
        if prev == 0:
            self.alive_since[(s, px)] = self.msg_sec
        if s == BID:
            if px > self.best[BID]:
                self.best[BID] = px
        elif self.best[ASK] == 0 or px < self.best[ASK]:
            self.best[ASK] = px

    def remove(self, oid, qty=None, record_fill=False):
        rec = self.orders.get(oid)
        if rec is None:
            return
        px, sz, s = rec
        take = sz if qty is None else min(qty, sz)
        lv = self.levels[s]
        rem = lv.get(px, 0) - take
        if record_fill:
            # (sec, fill_size, displayed_after): the post-fill displayed size
            # feeds the amendment's refill-ratio (consumed vs displayed) test.
            self.fills.setdefault((s, px), deque()).append(
                (self.msg_sec, take, max(rem, 0)))
        if rem > 0:
            lv[px] = rem
        else:
            lv.pop(px, None)
            self.alive_since.pop((s, px), None)      # level died: no longer alive
            if px == self.best[s]:
                lv2 = self.levels[s]
                self.best[s] = (max(lv2) if s == BID else min(lv2)) if lv2 else 0
        if take >= sz:
            del self.orders[oid]
        else:
            self.orders[oid] = (px, sz - take, s)

    def clear(self):
        self.orders.clear()
        self.levels[BID].clear()
        self.levels[ASK].clear()
        self.best[BID] = self.best[ASK] = 0
        self.alive_since.clear()
        self.fills.clear()

    def absorption_at(self, s, px, now_sec):
        """Frozen detector test for one (side, price) — amendment 79680f8:
        volume ≥ 300 across ≥ 5 fills, level alive 120 s, AND consumed volume
        ≥ 3× the max displayed size observed at the level (iceberg refill)."""
        alive = self.alive_since.get((s, px))
        if alive is None or now_sec - alive < ABS_WINDOW_S:
            return False                              # not continuously alive 120 s
        dq = self.fills.get((s, px))
        if not dq:
            return False
        cutoff = now_sec - ABS_WINDOW_S
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        if len(dq) < ABS_MIN_EVENTS:
            return False
        vol = sum(x[1] for x in dq)
        if vol < ABS_MIN_VOL:
            return False
        max_disp = max(max(x[2] for x in dq), self.levels[s].get(px, 0))
        return vol >= ABS_REFILL_RATIO * max(max_disp, 1)


def _day_bounds(sec):
    """RTH entry window + flatten second for the ET day containing `sec`,
    plus the epoch second of the NEXT ET midnight (the correct cache-roll
    boundary — keying the cache on the UTC day recomputes bounds at 19:00 ET
    the previous evening and then evaluates every RTH second against an
    already-expired window; that bug produced zero scans on first run)."""
    dt = datetime.fromtimestamp(sec, tz=ET)
    def at(hh, mm):
        return int(dt.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp())
    nxt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return at(*ENTRY_FIRST), at(*ENTRY_LAST), at(*FLATTEN), int(nxt.timestamp())


def run_window(path: Path, max_msgs: int | None = None) -> dict:
    bk = Book()
    store = db.DBNStore.from_file(path)
    t0 = _time.time()
    n_msgs = 0

    mid_win: deque[float] = deque()
    cur_sec = -1
    day_roll = -1                 # epoch sec of next ET midnight (bounds cache)
    e_first = e_last = flat_sec = 0
    rearm: dict[tuple[int, int], int] = {}

    state = "flat"          # flat | open
    side = 0
    entry_px = 0.0
    stop_px = tgt_px = 0.0
    open_sec = 0
    pend = None             # (side, signal_sec) awaiting next-snapshot entry

    trades: list[tuple[int, float, float, int, str, float]] = []
    signals = 0

    def book_exit(px, kind, sec):
        nonlocal state
        pnl = (px - entry_px) * side * PT_USD - COMM_RT - SLIP_USD
        trades.append((sec, entry_px, px, side, kind, pnl))
        state = "flat"

    def sample_through(target_sec):
        nonlocal cur_sec, day_roll, e_first, e_last, flat_sec, state, side
        nonlocal entry_px, stop_px, tgt_px, open_sec, pend, signals
        if target_sec - cur_sec > GAP_RESET_S:
            mid_win.clear()
            cur_sec = target_sec
            pend = None
            return
        while cur_sec < target_sec:
            sec = cur_sec
            cur_sec += 1
            bb, ba = bk.best
            if not (bb and ba and ba > bb):
                continue
            mid = (bb + ba) / 2 * PX
            mid_win.append(mid)
            if len(mid_win) > SIGMA_WINDOW:
                mid_win.popleft()

            if sec >= day_roll:
                e_first, e_last, flat_sec, day_roll = _day_bounds(sec)

            if state == "open":
                bid_px, ask_px = bb * PX, ba * PX
                out = bid_px if side > 0 else ask_px
                if side > 0 and bid_px <= stop_px:
                    book_exit(out, "stop", sec)
                elif side < 0 and ask_px >= stop_px:
                    book_exit(out, "stop", sec)
                elif side > 0 and bid_px >= tgt_px:
                    book_exit(out, "target", sec)
                elif side < 0 and ask_px <= tgt_px:
                    book_exit(out, "target", sec)
                elif sec - open_sec >= MAX_HOLD_S or sec >= flat_sec:
                    book_exit(out, "timeout", sec)
                continue

            # pending entry: fill as taker at this snapshot
            if pend is not None:
                pside, _ = pend
                pend = None
                if len(mid_win) >= SIGMA_WINDOW and e_first <= sec <= e_last:
                    sig = float(np.std(np.fromiter(mid_win, float)))
                    if sig > 0:
                        tick = TICK_I * PX
                        stop_t = max(1, round(STOP_SIG * sig / tick))
                        tgt_t = max(1, round(TGT_SIG * sig / tick))
                        side = pside
                        entry_px = (ba if pside > 0 else bb) * PX
                        stop_px = entry_px - pside * stop_t * tick
                        tgt_px = entry_px + pside * tgt_t * tick
                        open_sec = sec
                        state = "open"
                continue

            # detector scan (only when flat, in entry window, warmed up)
            if not (e_first <= sec <= e_last) or len(mid_win) < SIGMA_WINDOW:
                continue
            hit_side = 0
            for s, touch, step in ((BID, bb, -TICK_I), (ASK, ba, TICK_I)):
                for k in range(ABS_MAX_TICKS_FROM_TOUCH + 1):
                    px = touch + step * k
                    key = (s, px)
                    if rearm.get(key, 0) > sec:
                        continue
                    if bk.absorption_at(s, px, sec):
                        rearm[key] = sec + REARM_S
                        hit_side = 1 if s == BID else -1
                        break
                if hit_side:
                    break
            if hit_side:
                signals += 1
                pend = (hit_side, sec)

    for arr in store.to_ndarray(count=CHUNK):
        ts = arr["ts_event"].astype(np.int64)
        act = arr["action"]
        side_c = arr["side"]
        px_a = arr["price"].astype(np.int64)
        sz_a = arr["size"].astype(np.int64)
        oid_a = arr["order_id"].astype(np.int64)
        n = len(ts)
        for i in range(n):
            sec = int(ts[i]) // 1_000_000_000
            bk.msg_sec = sec
            if cur_sec == -1:
                cur_sec = sec
            elif sec > cur_sec:
                sample_through(sec)
            a = act[i]
            oid = int(oid_a[i])
            if a == b"A":
                s = BID if side_c[i] == b"B" else (ASK if side_c[i] == b"A" else -1)
                if s >= 0:
                    bk.add(oid, int(px_a[i]), int(sz_a[i]), s)
            elif a == b"C":
                bk.remove(oid)
            elif a == b"M":
                rec = bk.orders.get(oid)
                if rec is not None:
                    bk.remove(oid)
                    s = BID if side_c[i] == b"B" else (ASK if side_c[i] == b"A" else -1)
                    if s >= 0:
                        bk.add(oid, int(px_a[i]), int(sz_a[i]), s)
            elif a == b"F":
                bk.remove(oid, qty=int(sz_a[i]), record_fill=True)
            elif a == b"R":
                bk.clear()
        n_msgs += n
        rate = n_msgs / max(_time.time() - t0, 1e-9)
        print(f"    …{n_msgs/1e6:.0f}M msgs ({rate/1e6:.2f}M/s) signals={signals} "
              f"trades={len(trades)}", flush=True)
        # hygiene: prune stale fill deques so memory stays bounded
        if n_msgs % (CHUNK * 5) < CHUNK:
            cutoff = cur_sec - 2 * ABS_WINDOW_S
            for key in [k for k, dq in bk.fills.items()
                        if not dq or dq[-1][0] < cutoff]:
                del bk.fills[key]
        if max_msgs and n_msgs >= max_msgs:
            break

    pnls = np.array([t[5] for t in trades])
    res = {"n_msgs": n_msgs, "signals": signals,
           "stop_exits": sum(1 for t in trades if t[4] == "stop"),
           "target_exits": sum(1 for t in trades if t[4] == "target"),
           "timeout_exits": sum(1 for t in trades if t[4] == "timeout")}
    if len(pnls):
        gp, gl = pnls[pnls > 0].sum(), pnls[pnls <= 0].sum()
        res.update({"n": int(len(pnls)),
                    "total_usd": round(float(pnls.sum()), 2),
                    "avg_usd": round(float(pnls.mean()), 4),
                    "pf": round(float(gp / -gl), 3) if gl < 0 else None})
        if len(pnls) > 2 and pnls.std(ddof=1) > 0:
            t = float(pnls.mean() / (pnls.std(ddof=1) / np.sqrt(len(pnls))))
            res["t"] = round(t, 3)
            res["p_one_sided"] = round(1 - 0.5 * (1 + erf(t / sqrt(2))), 5)
        rng = np.random.default_rng(RNG_SEED)
        res["p_bootstrap"] = round(float(
            (rng.choice(pnls, size=(BOOT_N, len(pnls)), replace=True)
             .mean(axis=1) <= 0).mean()), 5)
    return res


def judge(r: dict) -> str:
    n = r.get("n", 0)
    if n < 500:
        return "UNDERPOWERED"
    bar = (n >= 500 and (r.get("pf") or 0) >= 1.10
           and r.get("p_one_sided") is not None and r["p_one_sided"] < 0.05
           and r.get("p_bootstrap") is not None and r["p_bootstrap"] < 0.05)
    full = n >= 1000
    return ("PASS" if bar and full else
            ("PASS(n-floor-relaxed)" if bar else "FAIL"))


def main() -> int:
    smoke = "--smoke" in sys.argv
    results = {}
    for name, path in WINDOWS.items():
        print(f"── {name}: {path.name}", flush=True)
        results[name] = run_window(path, max_msgs=20_000_000 if smoke else None)
        results[name]["judged"] = judge(results[name])
        print(json.dumps(results[name], indent=1), flush=True)
        if smoke:
            break
    if not smoke:
        js = [r["judged"] for r in results.values()]
        if any(j == "UNDERPOWERED" for j in js):
            verdict = "UNDERPOWERED"
        elif all(j.startswith("PASS") for j in js):
            verdict = "PASS"
        else:
            verdict = "FAIL"
        out = {"registered": "Round 22 (commit 2abecea)",
               "verdict_both_windows": verdict, "windows": results}
        (HERE / "round22_results.json").write_text(json.dumps(out, indent=1))
        print(f"\nROUND 22 VERDICT (both windows): {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
