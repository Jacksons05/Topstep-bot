"""Screen (A): does Round 26's overnight-inventory reversion survive a
slippage-dodging DELAYED entry instead of the 09:30 opening-auction fill?

R26 mechanically passed at 1-tick but died at the honest 2-tick open fill
(t 2.33 -> 0.91) and was regime-concentrated (all profit 2020+). This changes
ONE thing vs the frozen R26 spec: the entry bar. Everything else — top-tercile
|overnight move| signal, fade direction, target = prior RTH close, stop = entry
∓ 1·ATR(14), valid-geometry-only, both-hit=stop, 15:55 flatten — is identical,
reusing the same candidates.evaluate cost/stat kernel.

Three questions:
  1. Does the edge survive when we DON'T enter at the chaotic open?
  2. Delayed entries have tighter spreads -> 1-tick is honest again. Compare
     net at delayed/1-tick vs the open/2-tick that killed R26.
  3. Does it exist in BOTH decades, or is it still a post-2020 artifact?

Diagnostic: how much of the reversion is already gone by 09:45 / 10:00 (if the
snap-back happens in the first 15 min, a delayed entry throws the edge away).

Not a registered round — a pre-decision screen. Judged on ES (MES reported).
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402
from round25_26_auction_inventory import _rth_days, _simulate_exit  # noqa: E402

ENTRY_MINS = {
    "09:30 (R26 open)": 9 * 60 + 30,
    "09:45": 9 * 60 + 45,
    "10:00": 10 * 60,
    "10:15": 10 * 60 + 15,
}


def build(sym):
    """Load once; return everything the per-entry loop needs."""
    ts, o, h, l, c, v = C.load(sym)
    atr = C._atr(h, l, c)
    by_day = _rth_days(ts)
    days = sorted(by_day)
    rth_close = {d: c[by_day[d][-1]] for d in days}
    rth_open = {d: o[by_day[d][0]] for d in days}
    prev = {d: days[i - 1] for i, d in enumerate(days) if i > 0}
    return dict(ts=ts, o=o, h=h, l=l, c=c, atr=atr, by_day=by_day, days=days,
                rth_close=rth_close, rth_open=rth_open, prev=prev)


def run_entry(B, entry_min):
    ts, o, h, l, c, atr = B["ts"], B["o"], B["h"], B["l"], B["c"], B["atr"]
    by_day, days = B["by_day"], B["days"]
    rth_close, rth_open, prev = B["rth_close"], B["rth_open"], B["prev"]

    on_hist = []
    trades = []
    stats = {"days": 0, "signals": 0, "geom_skip": 0, "no_entry_bar": 0}
    revert_frac = []   # fraction of the overnight move already retraced by entry_min
    for d in days:
        pd = prev.get(d)
        if pd is None:
            continue
        on = rth_open[d] - rth_close[pd]
        stats["days"] += 1
        thr = np.percentile([abs(x) for x in on_hist[-60:]], 66.6667) if len(on_hist) >= 60 else None
        on_hist.append(on)
        if thr is None or abs(on) < thr or abs(on) <= 0:
            continue
        idxs = by_day[d]
        # entry bar = first RTH bar at/after entry_min, with room to manage after it
        k = next((kk for kk, i in enumerate(idxs)
                  if C.mins(ts[i]) >= entry_min and kk < len(idxs) - 1), None)
        if k is None:
            stats["no_entry_bar"] += 1
            continue
        ei = idxs[k]
        a = atr[ei]
        if np.isnan(a) or a <= 0:
            continue
        side = -1 if on > 0 else 1            # fade the overnight move
        epx = o[ei]                            # honest next-open fill at entry bar
        tgt = rth_close[pd]                    # revert to pre-overnight level
        stop = epx + (1.0 * a if side < 0 else -1.0 * a)
        # reversion already realized by this bar's open (relative to the 09:30 open)
        denom = rth_open[d] - rth_close[pd]
        if denom != 0:
            revert_frac.append((rth_open[d] - epx) / denom)
        if side < 0 and not (tgt < epx < stop):
            stats["geom_skip"] += 1
            continue
        if side > 0 and not (stop < epx < tgt):
            stats["geom_skip"] += 1
            continue
        stats["signals"] += 1
        ex_i, ex_px = _simulate_exit(ts, idxs, k, side, epx, stop, tgt, o, h, l, c)
        trades.append((ei, ex_i, epx, ex_px, side))
    stats["revert_frac_by_entry"] = round(float(np.mean(revert_frac)), 3) if revert_frac else None
    return trades, stats


def decade_split(cell):
    """(pre-2020 total, 2020+ total) from the yearly breakdown."""
    y = cell.get("yearly_usd", {})
    pre = round(sum(v for k, v in y.items() if int(k) < 2020), 0)
    post = round(sum(v for k, v in y.items() if int(k) >= 2020), 0)
    return pre, post


def main():
    for sym in ("ES", "MES"):
        print(f"\n{'='*78}\n  {sym}  — overnight-inventory reversion, entry-timing x slippage screen\n{'='*78}")
        B = build(sym)
        print(f"{'entry':<18}{'slip':>5}{'n':>6}{'PF':>7}{'t':>7}{'p':>9}"
              f"{'yrs+':>7}{'total$':>11}{'pre2020':>10}{'post2020':>11}  reverted@entry")
        for label, em in ENTRY_MINS.items():
            trades, stats = run_entry(B, em)
            rf = stats["revert_frac_by_entry"]
            for slip in (1, 2):
                C.SLIP_TICKS = slip
                cell = C.evaluate(trades, B["ts"], sym)
                if cell.get("n", 0) == 0:
                    print(f"{label:<18}{slip:>5}{0:>6}   (no trades)")
                    continue
                pre, post = decade_split(cell)
                rf_str = f"{rf:+.2f}" if (slip == 1 and rf is not None) else ""
                print(f"{label:<18}{slip:>5}{cell['n']:>6}{cell.get('pf') or 0:>7.3f}"
                      f"{cell.get('t') or 0:>7.2f}{cell.get('p_one_sided') or 1:>9.3f}"
                      f"{cell.get('pct_years_positive', 0):>6.0f}%{cell['total_usd']:>11,.0f}"
                      f"{pre:>10,.0f}{post:>11,.0f}  {rf_str}")
        C.SLIP_TICKS = 1  # reset
    print("\nreverted@entry = mean fraction of the overnight move already retraced by the")
    print("entry bar (1.0 => fully reverted before we enter; the edge would be gone).")
    print("\nPASS bar: n>=200, PF>=1.15, p<0.05, p_boot<0.05, yrs+>=60% (judged on ES).")


if __name__ == "__main__":
    main()
