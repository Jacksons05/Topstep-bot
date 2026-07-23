"""R32 (pre-registered 2026-07-23, oos/HYPOTHESES.md): commodity TSMOM on micros,
expressed as Topstep session-long holds.

PRIMARY: equal-weight 1-micro basket {CL, NG, GC, HG}, side = sign(past 60d return),
held every session (close-to-close proxy), net of micro costs. PASS = full-sample
t>=2 AND >=4/5 eras positive. Trials: lookbacks {20, 60, 252} (60d is primary).
Diagnostics (no selection): per-market panels, |ret|>8% roll-jump clip sensitivity.

Offline + deterministic: reads oos/data/free_daily/*.csv only. No RNG.
  .venv/bin/python oos/round32_commodity_tsmom.py
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
CACHE = HERE / "data" / "free_daily"

# micro specs: $ per 1.0 point of the quoted (full-size) price, per MICRO contract,
# and per-session cost = commission RT + 2 ticks/side spread-slippage (4 ticks RT).
MICROS = {
    #        $/pt(micro)  tick    $/tick(micro)  comm_rt
    "CL": {"pv": 100.0, "tick": 0.01,  "tick_usd": 1.00, "comm": 1.40},   # MCL
    "NG": {"pv": 1000.0, "tick": 0.001, "tick_usd": 1.00, "comm": 1.40},  # MNG
    "GC": {"pv": 10.0,  "tick": 0.10,  "tick_usd": 1.00, "comm": 1.40},   # MGC
    "HG": {"pv": 2500.0, "tick": 0.0005, "tick_usd": 1.25, "comm": 1.40}, # MHG
}
ERAS = [("2001-08", 2001, 2008), ("2009-15", 2009, 2015), ("2016-19", 2016, 2019),
        ("2020-21", 2020, 2021), ("2022-26", 2022, 2026)]
LOOKBACKS = [20, 60, 252]          # 60 = pre-registered primary
PRIMARY_LB = 60
CLIP = 0.08                         # roll-jump sensitivity clip


def load(root: str):
    rows = []
    with (CACHE / f"{root}.csv").open() as f:
        for r in csv.DictReader(f):
            try:
                rows.append((date.fromisoformat(r["date"]), float(r["close"])))
            except ValueError:
                continue
    rows.sort()
    d = np.array([x[0] for x in rows])
    c = np.array([x[1] for x in rows], float)
    keep = c > 0
    return d[keep], c[keep]


def era_of(y: int) -> str:
    for name, a, b in ERAS:
        if a <= y <= b:
            return name
    return "pre"


def per_market(root: str, lb: int, clip: float | None):
    """Daily $ P&L stream (per 1 micro) for sign(past-lb-return) held next session."""
    dts, cl = load(root)
    ret = np.diff(cl) / cl[:-1]
    if clip is not None:
        ret = np.clip(ret, -clip, clip)
    spec = MICROS[root]
    cost = spec["comm"] + 4 * spec["tick_usd"]          # RT comm + 4 ticks RT
    pnl, yrs, sides = [], [], []
    # signal at close t uses cl[t-lb..t]; position held over return t->t+1
    for t in range(lb, len(cl) - 1):
        mom = cl[t] - cl[t - lb]
        if mom == 0:
            continue
        side = 1.0 if mom > 0 else -1.0
        # $ pnl on 1 micro = side * (px_{t+1}-px_t) * pv  (clip applied via ret)
        r = ret[t]                                      # return t->t+1
        pnl.append(side * r * cl[t] * spec["pv"] - cost)
        yrs.append(dts[t + 1].year)
        sides.append(side)
    return np.asarray(pnl), np.asarray(yrs), np.asarray(sides)


def summarize(name: str, pnl: np.ndarray, yrs: np.ndarray) -> dict:
    m = pnl.mean(); sd = pnl.std()
    t = m / (sd / np.sqrt(len(pnl))) if sd > 0 else 0.0
    ann_sharpe = m / sd * np.sqrt(252) if sd > 0 else 0.0
    eras = []
    for ename, a, b in ERAS:
        mask = (yrs >= a) & (yrs <= b)
        eras.append((ename, pnl[mask].mean() if mask.any() else None))
    npos = sum(1 for _, v in eras if v is not None and v > 0)
    era_s = "/".join(f"{v:+.1f}" if v is not None else "--" for _, v in eras)
    print(f"  {name:<26} n={len(pnl):<6} mean${m:>+7.2f}/d  t={t:>+5.2f} "
          f"annSR={ann_sharpe:>+5.2f}  eras+ {npos}/5  [{era_s}]")
    return {"mean": m, "t": t, "npos": npos, "sharpe": ann_sharpe}


def basket(lb: int, clip: float | None):
    """Equal-weight: sum of the four per-market daily $ streams, date-aligned."""
    streams = {}
    for root in MICROS:
        dts, cl = load(root)
        ret = np.diff(cl) / cl[:-1]
        if clip is not None:
            ret = np.clip(ret, -clip, clip)
        spec = MICROS[root]
        cost = spec["comm"] + 4 * spec["tick_usd"]
        for t in range(lb, len(cl) - 1):
            mom = cl[t] - cl[t - lb]
            if mom == 0:
                continue
            side = 1.0 if mom > 0 else -1.0
            d = dts[t + 1]
            streams.setdefault(d, 0.0)
            streams[d] += side * ret[t] * cl[t] * spec["pv"] - cost
    days = sorted(streams)
    pnl = np.array([streams[d] for d in days])
    yrs = np.array([d.year for d in days])
    return pnl, yrs


def main():
    print("R32: commodity TSMOM basket {MCL,MNG,MGC,MHG}, session-hold proxy, net "
          "of micro costs (comm+4 ticks RT). Pre-registered primary: 60d lookback.")
    print("=" * 95)
    print("== BASKET (the pre-registered claim) ==")
    results = {}
    for lb in LOOKBACKS:
        pnl, yrs = basket(lb, clip=None)
        tag = " <== PRIMARY" if lb == PRIMARY_LB else ""
        results[lb] = summarize(f"basket lb={lb}{tag}", pnl, yrs)
    print("\n== SENSITIVITY: |ret|>8% clipped (roll-jump guard), primary lb only ==")
    pnl, yrs = basket(PRIMARY_LB, clip=CLIP)
    clip_res = summarize(f"basket lb={PRIMARY_LB} clipped", pnl, yrs)
    print("\n== PER-MARKET DIAGNOSTICS (no selection), lb=60, unclipped ==")
    for root in MICROS:
        p, y, s = per_market(root, PRIMARY_LB, clip=None)
        summarize(f"{root} (1 micro)", p, y)
        long_share = (s > 0).mean() * 100
        print(f"      {'':<22} long {long_share:.0f}% of days")
    print("\n== VERDICT (pre-registered criteria) ==")
    r = results[PRIMARY_LB]
    ok = r["t"] >= 2.0 and r["npos"] >= 4
    print(f"  primary lb=60: t={r['t']:+.2f} (need >=2), eras+ {r['npos']}/5 (need >=4)")
    print(f"  clip sensitivity: t={clip_res['t']:+.2f}, eras+ {clip_res['npos']}/5")
    print(f"  RESULT: {'SCREEN-PASS -> forward paper-log next (NOT live)' if ok else 'KILL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
