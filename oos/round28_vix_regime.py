"""Round 28 — VIX-regime-conditioned intraday behavior (ES). Frozen spec
registered in HYPOTHESES.md (commit 739ef8b), run on the SEARCH set only.

  * Regime(D) = prior trading day's VIXCLS close vs trailing 60 VIX closes
    strictly before D: top tercile -> HIGH, bottom tercile -> LOW, else SKIP.
  * morning move = sign(RTH 11:00 close - 09:30 open).
  * HIGH -> MOMENTUM (trade WITH the move); LOW -> REVERSION (trade OPPOSITE).
  * Enter first 5-min bar open >= 11:05 ET, exit 15:55 flatten. One trade/day.
  * SEARCH 2010-06..2025-06-04; HOLDOUT 2025-06-05..2026-06-05 = LOCKED (skipped).
  * Combined HIGH+LOW stream is the strategy. Mirror assignment = diagnostic only.
  * Net 1-tick (primary) + 2-tick; seed 7 bootstrap; deflated Sharpe N=31.

Usage: .venv/bin/python oos/round28_vix_regime.py
"""
from __future__ import annotations

import csv
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from statistics import NormalDist

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import candidates as C  # noqa: E402

RTH_OPEN, MORN_END, ENTRY_MIN, FLATTEN = 9 * 60 + 30, 11 * 60, 11 * 60 + 5, 15 * 60 + 55
HOLDOUT_START = date(2025, 6, 5)
N_TRIALS = 31
VIX_WIN = 60


def load_vix() -> dict[date, float]:
    out = {}
    with (HERE / "data" / "macro" / "VIXCLS.csv").open() as f:
        for row in csv.DictReader(f):
            try:
                out[date.fromisoformat(row["date"])] = float(row["value"])
            except (ValueError, KeyError):
                continue
    return out


def vix_regime(vix: dict[date, float]) -> dict[date, str]:
    """date -> 'HIGH'|'LOW'|'MID' using the PRIOR vix day vs its trailing 60."""
    vdays = sorted(vix)
    vals = [vix[d] for d in vdays]
    reg = {}
    for i in range(len(vdays)):
        if i < VIX_WIN:
            continue
        hist = vals[i - VIX_WIN:i]                 # strictly before this vix day
        v_prior = vals[i]                          # this vix close governs the NEXT session
        hi = np.percentile(hist, 66.6667)
        lo = np.percentile(hist, 33.3333)
        label = "HIGH" if v_prior >= hi else ("LOW" if v_prior <= lo else "MID")
        reg[vdays[i]] = label                      # keyed by the VIX day; applied to next trading day
    return reg


def rth_sessions(ts):
    by_day = defaultdict(list)
    for i, t in enumerate(ts):
        if t.weekday() < 5 and RTH_OPEN <= C.mins(t) <= FLATTEN:
            by_day[t.date()].append(i)
    return by_day


def deflated_sharpe(net: np.ndarray):
    n = len(net)
    mu, sd = net.mean(), net.std(ddof=1)
    if sd <= 0 or n < 3:
        return float("nan"), float("nan"), float("nan"), float("nan")
    SR = mu / sd
    z = (net - mu) / sd
    skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
    se = math.sqrt(max((1 - skew * SR + ((kurt - 1) / 4) * SR ** 2) / (n - 1), 1e-18))
    emc = 0.5772156649015329
    Nd = NormalDist()
    SR0 = se * ((1 - emc) * Nd.inv_cdf(1 - 1.0 / N_TRIALS)
                + emc * Nd.inv_cdf(1 - 1.0 / (N_TRIALS * math.e)))
    return SR, SR0, SR - SR0, Nd.cdf((SR - SR0) / se)


def build(sym, mirror=False):
    ts, o, h, l, c, v = C.load(sym)
    by_day = rth_sessions(ts)
    vix = load_vix()
    reg_by_vixday = vix_regime(vix)
    vdays = sorted(reg_by_vixday)

    def regime_for(session_day):
        # most recent VIX day strictly before the session day
        import bisect
        i = bisect.bisect_left(vdays, session_day) - 1
        return reg_by_vixday[vdays[i]] if i >= 0 else "MID"

    trades = {"HIGH": [], "LOW": []}
    for d in sorted(by_day):
        if d >= HOLDOUT_START:
            continue
        reg = regime_for(d)
        if reg == "MID":
            continue
        idxs = by_day[d]
        # morning move sign: 09:30 open -> 11:00 close
        open_i = idxs[0]
        morn = [i for i in idxs if C.mins(ts[i]) <= MORN_END]
        entry = next((i for i in idxs if C.mins(ts[i]) >= ENTRY_MIN and i != idxs[-1]), None)
        if not morn or entry is None:
            continue
        move = c[morn[-1]] - o[open_i]
        if move == 0:
            continue
        mdir = 1 if move > 0 else -1
        # HIGH -> momentum (with move); LOW -> reversion (against)
        side = mdir if reg == "HIGH" else -mdir
        if mirror:
            side = -side
        epx, xi, xpx = o[entry], idxs[-1], c[idxs[-1]]
        trades[reg].append((entry, xi, epx, xpx, side))
    return ts, trades


def evalcell(sym, trades, ts, slip):
    C.SLIP_TICKS = slip
    cell = C.evaluate(trades, ts, sym)
    C.SLIP_TICKS = 1
    return cell


def netarr(sym, trades, slip=1):
    spec = C.SPECS[sym]
    cost = spec["comm_rt"] + 2 * slip * spec["tick"] * spec["pt"]
    return np.array([(xp - ep) * side * spec["pt"] - cost for _, _, ep, xp, side in trades])


def main():
    print("=" * 80)
    print("  ROUND 28 — VIX-regime-conditioned intraday (ES) — SEARCH SET ONLY")
    print("  (holdout 2025-06-05..2026-06-05 LOCKED, not evaluated)")
    print("=" * 80)
    for sym in ("ES", "MES"):
        ts, tr = build(sym, mirror=False)
        combined = tr["HIGH"] + tr["LOW"]
        print(f"\n--- {sym} --- (HIGH n={len(tr['HIGH'])}, LOW n={len(tr['LOW'])})")
        for label, trades in (("COMBINED", combined), ("HIGH(momentum)", tr["HIGH"]),
                              ("LOW(reversion)", tr["LOW"])):
            if not trades:
                continue
            for slip in (1, 2):
                cell = evalcell(sym, trades, ts, slip)
                print(f"  [{label:<16} {slip}t] n={cell.get('n')} avg=${cell.get('avg_usd')} "
                      f"PF={cell.get('pf')} t={cell.get('t')} p={cell.get('p_one_sided')} "
                      f"boot={cell.get('p_bootstrap')} yrs+={cell.get('pct_years_positive')}%")
            if label == "COMBINED":
                SR, SR0, hair, dsr = deflated_sharpe(netarr(sym, trades))
                print(f"       Sharpe={SR:.4f} SR0(N={N_TRIALS})={SR0:.4f} "
                      f"deflated={hair:+.4f} DSR={dsr:.3f}")
        # mirror diagnostic
        _, trm = build(sym, mirror=True)
        mcomb = trm["HIGH"] + trm["LOW"]
        cm = evalcell(sym, mcomb, ts, 1)
        print(f"  [MIRROR combined 1t] n={cm.get('n')} avg=${cm.get('avg_usd')} "
              f"PF={cm.get('pf')} t={cm.get('t')} (diagnostic: if this also loses, no dir edge)")
    print("\nPASS (ES COMBINED 1t, SEARCH): n>=200, PF>=1.15, p<0.05 (t AND boot),")
    print("yrs+>=60%, deflated>0. Any fail -> KILL, holdout stays locked.")


if __name__ == "__main__":
    main()
