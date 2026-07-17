"""Round 23 Phase A (mechanical threshold fix) + Phase B (frozen sim).

Runs entirely off the reducer's compact artifacts (oos/data/r23/):
  Phase A: candidate counts + cum-fill distribution → cum_fill* =
           max(25, P75) per registration 18f21ac. < 8 candidates/session
           average → UNDERPOWERED, stop before any P&L.
  Phase B: frozen rules — signal at first cum_fill ≥ cum_fill* while the
           order rests within 4 ticks of its side's touch, RTH 09:35–15:30
           ET; taker entry at next 1 s snapshot's opposite touch; bracket
           1.0σ/1.5σ (trailing 1800-sample σ of mids), stop-first; 15-min
           timeout; flatten 15:55; one position at a time; one signal per
           order id; 10-min re-arm per price. MES economics: $5/pt, $1.40 RT
           commission + 1 tick/side slippage ($2.50).

Known approximation (disclosed): a candidate's price is recorded at first
qualification; later price-modifies of the same iceberg are not tracked.

Usage:  .venv/bin/python oos/round23_phase_ab.py
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from math import erf, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
R23 = HERE / "data" / "r23"
ET = ZoneInfo("America/New_York")

TICK_I = 250_000_000
PX = 1e-9
PT_USD = 5.0
COMM_RT = 1.40
SLIP_USD = 2.50

MIN_FILL = 25
VIABILITY_PER_SESSION = 8
MAX_TICKS_FROM_TOUCH = 4
SIGMA_N = 1800
STOP_SIG, TGT_SIG = 1.0, 1.5
MAX_HOLD_S = 900
REARM_S = 600
BOOT_N = 20_000
RNG_SEED = 7
BID, ASK = 0, 1


def day_files():
    days = sorted(p.stem.split("_")[-1] for p in R23.glob("ES_iceberg_*.json"))
    return [(d, R23 / f"ES_book1s_{d}.npz", R23 / f"ES_iceberg_{d}.json")
            for d in days if (R23 / f"ES_book1s_{d}.npz").exists()]


def rth_bounds(day: str):
    base = datetime.fromisoformat(day).replace(tzinfo=ET)
    def at(h, m):
        return int(base.replace(hour=h, minute=m).timestamp())
    return at(9, 35), at(15, 30), at(15, 55)


def phase_a(files):
    per_day = []
    fills = []
    for day, _, ice in files:
        cands = json.loads(ice.read_text())
        per_day.append(len(cands))
        fills.extend(v["steps"][-1][1] for v in cands.values() if v["steps"])
    avg = float(np.mean(per_day)) if per_day else 0.0
    thr = max(MIN_FILL, int(np.percentile(fills, 75))) if fills else MIN_FILL
    return {"sessions": len(per_day), "candidates_total": int(np.sum(per_day)),
            "candidates_per_session": round(avg, 1),
            "cum_fill_p50": int(np.percentile(fills, 50)) if fills else None,
            "cum_fill_p75": int(np.percentile(fills, 75)) if fills else None,
            "cum_fill_star": thr,
            "viable": avg >= VIABILITY_PER_SESSION}


def phase_b(files, cum_fill_star):
    trades = []
    signals = 0
    for day, book_p, ice_p in files:
        z = np.load(book_p)
        sec, bid, ask = z["sec"], z["bid"], z["ask"]
        if len(sec) < SIGMA_N + 10:
            continue
        mid = (bid + ask) / 2.0 * PX
        e_first, e_last, flat_s = rth_bounds(day)

        # signal times: first step >= cum_fill_star per candidate order
        cands = json.loads(ice_p.read_text())
        sigs = []
        for v in cands.values():
            for s_sec, cf in v["steps"]:
                if cf >= cum_fill_star:
                    sigs.append((s_sec, int(v["side"]), int(v["px"])))
                    break
        sigs.sort()

        rearm: dict[int, int] = {}
        busy_until = -1
        for s_sec, side_book, px_i in sigs:
            if not (e_first <= s_sec <= e_last) or s_sec < busy_until:
                continue
            if rearm.get(px_i, 0) > s_sec:
                continue
            i = int(np.searchsorted(sec, s_sec))
            if i >= len(sec) or sec[i] != s_sec or i < SIGMA_N or i + 1 >= len(sec):
                continue
            touch = bid[i] if side_book == BID else ask[i]
            if abs(px_i - touch) > MAX_TICKS_FROM_TOUCH * TICK_I:
                continue
            sig30 = float(np.std(mid[i - SIGMA_N:i]))
            if sig30 <= 0:
                continue
            signals += 1
            rearm[px_i] = s_sec + REARM_S
            side = 1 if side_book == BID else -1
            j = i + 1                                # next 1 s snapshot
            entry = (ask[j] if side > 0 else bid[j]) * PX
            tick = TICK_I * PX
            stop = entry - side * max(1, round(STOP_SIG * sig30 / tick)) * tick
            tgt = entry + side * max(1, round(TGT_SIG * sig30 / tick)) * tick
            exit_px = None
            kind = "timeout"
            k = j + 1
            while k < len(sec):
                s_now = int(sec[k])
                b_px, a_px = bid[k] * PX, ask[k] * PX
                out = b_px if side > 0 else a_px
                if side > 0 and b_px <= stop or side < 0 and a_px >= stop:
                    exit_px, kind = out, "stop"
                    break
                if side > 0 and b_px >= tgt or side < 0 and a_px <= tgt:
                    exit_px, kind = out, "target"
                    break
                if s_now - int(sec[j]) >= MAX_HOLD_S or s_now >= flat_s:
                    exit_px = out
                    break
                k += 1
            if exit_px is None:
                exit_px = (bid[-1] if side > 0 else ask[-1]) * PX
            pnl = (exit_px - entry) * side * PT_USD - COMM_RT - SLIP_USD
            trades.append((day, entry, exit_px, side, kind, pnl))
            busy_until = int(sec[min(k, len(sec) - 1)])
    return trades, signals


def evaluate(trades):
    pnls = np.array([t[5] for t in trades])
    res = {"n": int(len(pnls)),
           "stop_exits": sum(1 for t in trades if t[4] == "stop"),
           "target_exits": sum(1 for t in trades if t[4] == "target"),
           "timeout_exits": sum(1 for t in trades if t[4] == "timeout")}
    if len(pnls) == 0:
        return res
    gp, gl = pnls[pnls > 0].sum(), pnls[pnls <= 0].sum()
    res.update({"total_usd": round(float(pnls.sum()), 2),
                "avg_usd": round(float(pnls.mean()), 4),
                "pf": round(float(gp / -gl), 3) if gl < 0 else None,
                "win_pct": round(100 * float((pnls > 0).mean()), 1)})
    if len(pnls) > 2 and pnls.std(ddof=1) > 0:
        t = float(pnls.mean() / (pnls.std(ddof=1) / np.sqrt(len(pnls))))
        res["t"] = round(t, 3)
        res["p_one_sided"] = round(1 - 0.5 * (1 + erf(t / sqrt(2))), 5)
    rng = np.random.default_rng(RNG_SEED)
    res["p_bootstrap"] = round(float(
        (rng.choice(pnls, size=(BOOT_N, len(pnls)), replace=True)
         .mean(axis=1) <= 0).mean()), 5)
    return res


def main() -> int:
    files = day_files()
    if not files:
        print("no artifacts — run round23_reduce.py first")
        return 1
    a = phase_a(files)
    print("PHASE A:", json.dumps(a, indent=1))
    if not a["viable"]:
        out = {"registered": "Round 23 (18f21ac)", "phase_a": a,
               "verdict": "UNDERPOWERED (Phase A viability floor)"}
        (HERE / "round23_results.json").write_text(json.dumps(out, indent=1))
        print("VERDICT: UNDERPOWERED — defer to forward capture")
        return 0
    trades, signals = phase_b(files, a["cum_fill_star"])
    b = evaluate(trades)
    b["signals"] = signals
    n = b.get("n", 0)
    if n < 500:
        verdict = "UNDERPOWERED"
    else:
        ok = ((b.get("pf") or 0) >= 1.10
              and b.get("p_one_sided") is not None and b["p_one_sided"] < 0.05
              and b.get("p_bootstrap") is not None and b["p_bootstrap"] < 0.05)
        verdict = "PASS (promising-not-proven; forward window required)" if ok else "FAIL"
    out = {"registered": "Round 23 (18f21ac)", "phase_a": a, "phase_b": b,
           "verdict": verdict}
    (HERE / "round23_results.json").write_text(json.dumps(out, indent=1))
    print("PHASE B:", json.dumps(b, indent=1))
    print(f"ROUND 23 VERDICT: {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
