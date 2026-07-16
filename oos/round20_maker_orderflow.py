"""Round 20 — maker-side OBI/CVD with honest queue-position fill modeling.

Implements exactly the registered spec (HYPOTHESES.md Round 20) plus its
pre-run amendment (commit fb09cc4, frozen before either window was
processed). Streams MES MBO (GLBX.MDP3) in chunks, maintains the full
per-order book, samples 1-second features (OBI10 z, CVD5, mid), and runs
the passive-entry / passive-TP / taker-time-stop state machine with
queue-ahead accounting at order-id granularity.

Data-hygiene note (decided before running, conservative): a feed gap
> 120 s (maintenance halt, weekend) RESETS the rolling feature windows, so
no signal can fire until 30 full minutes of fresh 1-second samples exist
after every halt — stale flat samples never pollute the z-score.

Judged separately on each window (PASS bar: n >= 1000, PF >= 1.10,
one-sided p < 0.05 by t AND 20k bootstrap seed 7 — BOTH windows must pass).

Usage:
    .venv/bin/python oos/round20_maker_orderflow.py            # both windows
    .venv/bin/python oos/round20_maker_orderflow.py --smoke    # first 20M msgs of W1
"""
from __future__ import annotations

import json
import sys
import time as _time
from collections import deque
from math import erf, sqrt
from pathlib import Path

import numpy as np
import databento as db

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

WINDOWS = {
    "W1_2026-01": DATA / "MES_mbo_2026-01-06_2026-02-06.dbn.zst",
    "W2_2026-05": DATA / "MES_mbo_2026-05-06_2026-06-06.dbn.zst",
}

PX = 1e-9                 # Databento fixed-precision price scale
TICK_I = 250_000_000      # 0.25 in 1e-9 units (MES tick)
PT_USD = 5.0              # MES $/point
COMM_RT = 1.40            # micro round-trip commission (registered)
SLIP_TICK_USD = 0.25 * PT_USD  # 1 tick, taker time-stop exits only

Z_WINDOW = 1800           # 30 min of 1 s OBI samples
CVD_WINDOW = 300          # 5 min signed trade volume
Z_ENTRY = 1.5
REST_EXPIRY_S = 30        # unfilled entry order dies (no trade)
TIME_STOP_S = 300         # 5-min taker exit if TP never fills
GAP_RESET_S = 120         # feed gap that resets the feature windows
CHUNK = 4_000_000
BOOT_N = 20_000
RNG_SEED = 7

BID, ASK = 0, 1


class Sim:
    """Single-window event-driven simulator (plain Python, column-fed)."""

    def __init__(self):
        # book: order_id -> (price_i, size, side_int)
        self.orders: dict[int, tuple[int, int, int]] = {}
        self.levels = ({}, {})                 # (bid, ask): price_i -> size
        self.best = [0, 0]                     # best bid / best ask (price_i)
        # 1 s features
        self.obi_win: deque[float] = deque()
        self.mid_win: deque[float] = deque()
        self.cvd_win: deque[float] = deque()
        self.obi_sum = 0.0
        self.obi_sq = 0.0
        self.cur_sec = -1
        self.sec_cvd = 0.0
        # state machine: flat -> resting -> open_tp -> flat
        self.state = "flat"
        self.side = 0
        self.rest_px = -1                      # our resting price (entry or TP)
        self.ahead: dict[int, int] = {}
        self.rest_since = 0
        self.entry_px = 0.0
        self.fill_sec = 0
        self.tp_ticks = 1
        # results
        self.trades: list[tuple[int, float, float, int, str, float]] = []
        self.signals = 0
        self.expired = 0
        self.queue_secs: list[int] = []

    # ── rolling windows ──────────────────────────────────────────────────────
    def reset_windows(self):
        self.obi_win.clear()
        self.mid_win.clear()
        self.cvd_win.clear()
        self.obi_sum = self.obi_sq = 0.0
        self.sec_cvd = 0.0

    # ── book maintenance ────────────────────────────────────────────────────
    def add(self, oid, px, sz, s):
        self.orders[oid] = (px, sz, s)
        lv = self.levels[s]
        lv[px] = lv.get(px, 0) + sz
        if s == BID:
            if px > self.best[BID]:
                self.best[BID] = px
        elif self.best[ASK] == 0 or px < self.best[ASK]:
            self.best[ASK] = px

    def remove(self, oid, qty=None):
        rec = self.orders.get(oid)
        if rec is None:
            return
        px, sz, s = rec
        take = sz if qty is None else min(qty, sz)
        lv = self.levels[s]
        rem = lv.get(px, 0) - take
        if rem > 0:
            lv[px] = rem
        else:
            lv.pop(px, None)
            if px == self.best[s]:
                lv2 = self.levels[s]
                self.best[s] = (max(lv2) if s == BID else min(lv2)) if lv2 else 0
        if take >= sz:
            del self.orders[oid]
        else:
            self.orders[oid] = (px, sz - take, s)

    def clear_book(self):
        self.orders.clear()
        self.levels[BID].clear()
        self.levels[ASK].clear()
        self.best[BID] = self.best[ASK] = 0
        if self.state == "resting":            # session clear kills the entry order
            self.state = "flat"
            self.ahead.clear()
        elif self.state == "open_tp":          # TP order dies; timestop will exit
            self.ahead.clear()
            self.rest_px = -1

    def obi10(self):
        bids, asks = self.levels
        bb, ba = self.best
        if bb == 0 or ba == 0 or ba <= bb:
            return None
        bs = ask_s = 0
        px, found, step = bb, 0, 0
        while found < 10 and step < 400:
            v = bids.get(px)
            if v:
                bs += v
                found += 1
            px -= TICK_I
            step += 1
        px, found, step = ba, 0, 0
        while found < 10 and step < 400:
            v = asks.get(px)
            if v:
                ask_s += v
                found += 1
            px += TICK_I
            step += 1
        d = bs + ask_s
        return (bs - ask_s) / d if d > 0 else None

    # ── queue-ahead (amendment §2) ───────────────────────────────────────────
    def join_queue(self, px_i, side_book, sec):
        self.ahead = {oid: rec[1] for oid, rec in self.orders.items()
                      if rec[0] == px_i and rec[2] == side_book}
        self.rest_px = px_i
        self.rest_since = sec

    def ahead_fill(self, oid, qty):
        old = self.ahead[oid]
        if qty >= old:
            del self.ahead[oid]
        else:
            self.ahead[oid] = old - qty

    def ahead_modify(self, oid, new_px, new_sz):
        old = self.ahead.pop(oid)
        if new_px == self.rest_px and new_sz < old:
            self.ahead[oid] = new_sz           # size cut keeps priority
        # size raise or price move: stays dropped (priority lost)


def run_window(path: Path, max_msgs: int | None = None) -> dict:
    sim = Sim()
    store = db.DBNStore.from_file(path)
    t0 = _time.time()
    n_msgs = 0

    def sample_through(target_sec):
        """Advance the 1 s clock to target_sec (exclusive), sampling each second."""
        if target_sec - sim.cur_sec > GAP_RESET_S:
            sim.reset_windows()                # halt/weekend: no stale samples
            sim.cur_sec = target_sec
            if sim.state == "resting":         # order idled through a halt: expire
                sim.state = "flat"
                sim.ahead.clear()
                sim.expired += 1
            return
        while sim.cur_sec < target_sec:
            sec = sim.cur_sec
            sim.cur_sec += 1
            # close this second's CVD bucket
            sim.cvd_win.append(sim.sec_cvd)
            sim.sec_cvd = 0.0
            if len(sim.cvd_win) > CVD_WINDOW:
                sim.cvd_win.popleft()

            obi = sim.obi10()
            bb, ba = sim.best
            mid = (bb + ba) / 2 * PX if (bb and ba and ba > bb) else None
            if obi is None or mid is None:
                continue
            if len(sim.obi_win) == Z_WINDOW:
                old = sim.obi_win.popleft()
                sim.mid_win.popleft()
                sim.obi_sum -= old
                sim.obi_sq -= old * old
            sim.obi_win.append(obi)
            sim.mid_win.append(mid)
            sim.obi_sum += obi
            sim.obi_sq += obi * obi

            # ── state machine on the 1 s grid ────────────────────────────
            if sim.state == "resting" and sec - sim.rest_since >= REST_EXPIRY_S:
                sim.state = "flat"
                sim.ahead.clear()
                sim.expired += 1
            if sim.state == "open_tp" and sec - sim.fill_sec >= TIME_STOP_S:
                if bb and ba and ba > bb:      # valid book (amendment §5)
                    px = (bb if sim.side > 0 else ba) * PX
                    pnl = ((px - sim.entry_px) * sim.side * PT_USD
                           - COMM_RT - SLIP_TICK_USD)
                    sim.trades.append((sec, sim.entry_px, px, sim.side,
                                       "timestop", pnl))
                    sim.state = "flat"
                    sim.ahead.clear()
                continue
            if sim.state != "flat" or len(sim.obi_win) < Z_WINDOW:
                continue

            # signal (Round 5 params, frozen)
            mean = sim.obi_sum / Z_WINDOW
            var = sim.obi_sq / Z_WINDOW - mean * mean
            if var <= 0:
                continue
            z = (obi - mean) / sqrt(var)
            cvd5 = sum(sim.cvd_win)
            side = 1 if (z >= Z_ENTRY and cvd5 > 0) else \
                (-1 if (z <= -Z_ENTRY and cvd5 < 0) else 0)
            if side == 0:
                continue
            sim.signals += 1
            sim.side = side
            sim.join_queue(bb if side > 0 else ba,
                           BID if side > 0 else ASK, sec)
            sigma30 = float(np.std(np.fromiter(sim.mid_win, float)))
            move = abs(z) * sigma30
            sim.tp_ticks = max(1, int(round(move / (TICK_I * PX))))
            sim.state = "resting"

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
            if sim.cur_sec == -1:
                sim.cur_sec = sec
            elif sec > sim.cur_sec:
                sample_through(sec)

            a = act[i]
            oid = int(oid_a[i])
            if a == b"A":
                s = BID if side_c[i] == b"B" else (ASK if side_c[i] == b"A" else -1)
                if s >= 0:
                    sim.add(oid, int(px_a[i]), int(sz_a[i]), s)
            elif a == b"C":
                if oid in sim.ahead:
                    del sim.ahead[oid]
                sim.remove(oid)
            elif a == b"M":
                px, sz = int(px_a[i]), int(sz_a[i])
                rec = sim.orders.get(oid)
                if rec is not None:
                    if oid in sim.ahead:
                        sim.ahead_modify(oid, px, sz)
                    sim.remove(oid)
                    s = BID if side_c[i] == b"B" else (ASK if side_c[i] == b"A" else -1)
                    if s >= 0:
                        sim.add(oid, px, sz, s)
            elif a == b"F":
                sz = int(sz_a[i])
                if sim.state != "flat":
                    if oid in sim.ahead:
                        sim.ahead_fill(oid, sz)
                    else:
                        rec = sim.orders.get(oid)
                        if (rec is not None and rec[0] == sim.rest_px
                                and not sim.ahead):
                            want = (BID if sim.side > 0 else ASK) \
                                if sim.state == "resting" \
                                else (ASK if sim.side > 0 else BID)
                            if rec[2] == want:
                                if sim.state == "resting":
                                    sim.entry_px = sim.rest_px * PX
                                    sim.fill_sec = sim.cur_sec
                                    sim.queue_secs.append(sim.cur_sec - sim.rest_since)
                                    tp_i = sim.rest_px + sim.side * sim.tp_ticks * TICK_I
                                    sim.join_queue(tp_i, ASK if sim.side > 0 else BID,
                                                   sim.cur_sec)
                                    sim.state = "open_tp"
                                else:           # passive TP filled
                                    px_out = sim.rest_px * PX
                                    pnl = ((px_out - sim.entry_px) * sim.side
                                           * PT_USD - COMM_RT)
                                    sim.trades.append((sim.cur_sec, sim.entry_px,
                                                       px_out, sim.side, "tp", pnl))
                                    sim.state = "flat"
                                    sim.ahead.clear()
                sim.remove(oid, qty=sz)
            elif a == b"T":
                if side_c[i] == b"B":
                    sim.sec_cvd += int(sz_a[i])
                elif side_c[i] == b"A":
                    sim.sec_cvd -= int(sz_a[i])
            elif a == b"R":
                sim.clear_book()
        n_msgs += n
        rate = n_msgs / max(_time.time() - t0, 1e-9)
        print(f"    …{n_msgs/1e6:.0f}M msgs ({rate/1e6:.2f}M/s) signals={sim.signals} "
              f"fills={len(sim.trades)} expired={sim.expired}", flush=True)
        if max_msgs and n_msgs >= max_msgs:
            break

    pnls = np.array([t[5] for t in sim.trades])
    res = {"n_msgs": n_msgs, "signals": sim.signals,
           "expired_unfilled": sim.expired,
           "fills": len(sim.trades),
           "fill_rate_pct": round(100 * len(sim.trades) / sim.signals, 1)
           if sim.signals else None,
           "median_queue_s": float(np.median(sim.queue_secs))
           if sim.queue_secs else None,
           "tp_exits": sum(1 for t in sim.trades if t[4] == "tp"),
           "timestop_exits": sum(1 for t in sim.trades if t[4] == "timestop")}
    if len(pnls):
        gp, gl = pnls[pnls > 0].sum(), pnls[pnls <= 0].sum()
        res.update({
            "n": int(len(pnls)),
            "total_usd": round(float(pnls.sum()), 2),
            "avg_usd": round(float(pnls.mean()), 4),
            "pf": round(float(gp / -gl), 3) if gl < 0 else None,
        })
        if len(pnls) > 2 and pnls.std(ddof=1) > 0:
            t = float(pnls.mean() / (pnls.std(ddof=1) / np.sqrt(len(pnls))))
            res["t"] = round(t, 3)
            res["p_one_sided"] = round(1 - 0.5 * (1 + erf(t / sqrt(2))), 5)
        rng = np.random.default_rng(RNG_SEED)
        res["p_bootstrap"] = round(float(
            (rng.choice(pnls, size=(BOOT_N, len(pnls)), replace=True)
             .mean(axis=1) <= 0).mean()), 5)
    return res


def cell_passes(r: dict) -> bool:
    return bool(r.get("n", 0) >= 1000 and (r.get("pf") or 0) >= 1.10
                and r.get("p_one_sided") is not None and r["p_one_sided"] < 0.05
                and r.get("p_bootstrap") is not None and r["p_bootstrap"] < 0.05)


def main() -> int:
    smoke = "--smoke" in sys.argv
    results = {}
    for name, path in WINDOWS.items():
        print(f"── {name}: {path.name}", flush=True)
        results[name] = run_window(path, max_msgs=20_000_000 if smoke else None)
        print(json.dumps(results[name], indent=1), flush=True)
        if smoke:
            break
    if not smoke:
        verdict = ("PASS" if all(cell_passes(r) for r in results.values())
                   else "FAIL")
        out = {"registered": "Round 20 + amendment fb09cc4",
               "verdict_both_windows": verdict, "windows": results}
        (HERE / "round20_results.json").write_text(json.dumps(out, indent=1))
        print(f"\nROUND 20 VERDICT (both windows must pass): {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
