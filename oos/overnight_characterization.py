"""Characterize the overnight drift (descriptive — NOT a validation; the holdout is
already spent). Answers: is it real overnight-vs-intraday ALPHA (up overnight, flat/
down intraday = Lou-Polk-Skouras "tug of war") or just bull-market BETA captured
overnight? Plus regime (VIX / year) and tail structure, for a fork-B decision.

Segments per session (ET), using the Topstep-tradeable overnight:
  overnight = open(09:30 D) / open(18:00 D-1)     [our R30 trade]
  intraday  = close(16:00 D) / open(09:30 D)      [RTH]
No new pass/fail, no holdout — full sample, purely descriptive.

Usage: .venv/bin/python oos/overnight_characterization.py
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

EVE, OPEN, CLOSE = 18 * 60, 9 * 60 + 30, 16 * 60


def load_vix():
    out = {}
    p = HERE / "data" / "macro" / "VIXCLS.csv"
    with p.open() as f:
        for row in csv.DictReader(f):
            try:
                out[date.fromisoformat(row["date"])] = float(row["value"])
            except (ValueError, KeyError):
                pass
    return out


def bar_at(ts, idxs, m):
    return next((i for i in idxs if C.mins(ts[i]) >= m), None)


def segments(sym):
    ts, o, h, l, c, v = C.load(sym)
    by_date = defaultdict(list)
    for i, t in enumerate(ts):
        by_date[t.date()].append(i)
    rows = []  # (D, overnight_logret, intraday_logret)
    for E in sorted(by_date):
        if E.weekday() not in (6, 0, 1, 2, 3):
            continue
        D = E + timedelta(days=1)
        if D not in by_date:
            continue
        e18 = bar_at(ts, by_date[E], EVE)
        d0930 = bar_at(ts, by_date[D], OPEN)
        d1600 = bar_at(ts, by_date[D], CLOSE)
        if None in (e18, d0930) or d1600 is None:
            continue
        on = np.log(o[d0930] / o[e18])
        intra = np.log(c[d1600] / o[d0930])
        rows.append((D, on, intra))
    return rows


def main():
    vix = load_vix()
    vdays = sorted(vix)
    import bisect

    def prior_vix(d):
        i = bisect.bisect_left(vdays, d) - 1
        return vix[vdays[i]] if i >= 0 else None

    for sym in ("ES", "MNQ"):
        rows = segments(sym)
        D = np.array([r[0] for r in rows])
        on = np.array([r[1] for r in rows])
        intra = np.array([r[2] for r in rows])
        print("=" * 74)
        print(f"  {sym} — overnight drift characterization  (n={len(rows)} sessions, "
              f"{rows[0][0]}..{rows[-1][0]})")
        print("=" * 74)
        # 1. ALPHA vs BETA: cumulative overnight vs intraday
        cum_on, cum_in = on.sum(), intra.sum()
        print(f"  [decomp] cumulative log-return  overnight={cum_on:+.3f} "
              f"({np.expm1(cum_on)*100:+.0f}%)   intraday={cum_in:+.3f} "
              f"({np.expm1(cum_in)*100:+.0f}%)   total(on+intra)={cum_on+cum_in:+.3f}")
        share = cum_on / (cum_on + cum_in) if (cum_on + cum_in) != 0 else float("nan")
        print(f"           overnight share of total = {share*100:.0f}%   "
              f"corr(overnight, same-day intraday) = {np.corrcoef(on, intra)[0,1]:+.3f}")
        print(f"           mean/session: overnight={on.mean()*1e4:+.1f}bps "
              f"intraday={intra.mean()*1e4:+.1f}bps   "
              f"overnight win={(on>0).mean()*100:.0f}% intraday win={(intra>0).mean()*100:.0f}%")
        # 2. by year
        yrs = defaultdict(list)
        for d, x in zip(D, on):
            yrs[d.year].append(x)
        pos = sum(1 for y in yrs.values() if np.sum(y) > 0)
        print(f"  [by year] overnight positive in {pos}/{len(yrs)} years; per-year sum "
              f"range {min(np.sum(v) for v in yrs.values())*100:+.0f}% .. "
              f"{max(np.sum(v) for v in yrs.values())*100:+.0f}%")
        # 3. by VIX regime (prior-day VIX terciles over full sample)
        pv = np.array([prior_vix(d) or np.nan for d in D])
        ok = ~np.isnan(pv)
        lo, hi = np.nanpercentile(pv, 33.33), np.nanpercentile(pv, 66.67)
        for name, mask in (("LOW-VIX", ok & (pv <= lo)), ("MID", ok & (pv > lo) & (pv < hi)),
                           ("HIGH-VIX", ok & (pv >= hi))):
            seg = on[mask]
            print(f"  [VIX {name:<8}] n={seg.size} mean={seg.mean()*1e4:+.1f}bps "
                  f"win={(seg>0).mean()*100:.0f}% sum={seg.sum()*100:+.0f}%")
        # 4. tail (in $ per contract)
        spec = C.SPECS[sym]
        dollar = (np.expm1(on)) * 0.0  # placeholder
        # approximate $ move per contract ~ index_pts * pt; use on*price. Use realized:
        # reconstruct entry price not stored; approximate with pt * (exp(on)-1) * typical px
        # Simpler: report the log-return tail in bps.
        print(f"  [tail] overnight bps: worst={on.min()*1e4:.0f}  1st-pct={np.percentile(on,1)*1e4:.0f}  "
              f"5th-pct={np.percentile(on,5)*1e4:.0f}  95th={np.percentile(on,95)*1e4:.0f}  best={on.max()*1e4:.0f}")
        print()


if __name__ == "__main__":
    main()
