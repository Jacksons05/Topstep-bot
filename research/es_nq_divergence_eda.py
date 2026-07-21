"""ES-NQ intraday divergence EDA: does the spread mean-revert or trend?

Decisive pre-backtest test for the "fade the QQQ-SPY divergence" hypothesis,
run on the instruments we'd actually trade: MES (ES/S&P) and MNQ (NQ/Nasdaq)
5-min RTH bars. The economic object is the intraday tech-vs-broad factor.

We test the SPREAD process directly, so the result does not depend on any
entry-rule tuning:

  s_t  = logret(MNQ)_t - logret(MES)_t          (spread increment, within session)
  D_t  = cumsum(s) within session               (= log(NQ_t/NQ_open) - log(ES_t/ES_open))

  1. Variance ratio VR(q) on s_t, Lo-MacKinlay heteroskedasticity-robust z.
        VR<1 mean-reversion | VR=1 random walk | VR>1 trending
     Session-aware: q-windows never cross a session boundary.
  2. OU/AR(1) half-life of D_t (within-session pairs).
  3. Autocorrelation of s_t, lags 1..12.
  4. Era split (regime robustness / decay).
  5. Indicative payoff of the literal rule (z>=2 fade, exit |z|<=0.5 / 60min / EOD)
     for a ~dollar-neutral 1 MES + 1 MNQ pair, net of assumed retail friction.
     This is a signal-payoff study, NOT the full Topstep sim.

Reproducible: fixed seed for the block-bootstrap VR CI. No look-ahead: z uses a
strictly trailing within-session window; forward payoff uses only future bars.
"""
from __future__ import annotations

import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import research.datasets as ds  # noqa: E402

SEED = 20260720
BAR_MIN = 5
HORIZON_BARS = 12          # 60 min exit cap
Z_ENTRY = 2.0
Z_EXIT = 0.5
Z_WINDOW = 30              # trailing bars for the z-score (spec: 2.5h)
MIN_SESSION_BARS = 30      # skip half-days / thin holidays


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_sided_p(z: float) -> float:
    return 2.0 * (1.0 - _ncdf(abs(z)))


def build_sessions():
    """Aligned per-session close arrays for MES and MNQ over common RTH bars.

    Returns list of dicts: {date, mes[], mnq[]} with bars aligned by exact
    timestamp, ascending, RTH only, min length enforced.
    """
    mes = ds.load_bars("MES")
    mnq = ds.load_bars("MNQ")
    mes_c = {mes.ts[i]: mes.c[i] for i in range(len(mes))
             if mes.ts[i].weekday() < 5
             and ds.RTH_OPEN_MIN <= mes.minute_of_day(i) < ds.RTH_CLOSE_MIN}
    mnq_by_day = mnq.rth_sessions()
    out = []
    for day, idxs in sorted(mnq_by_day.items()):
        rows = []
        for i in idxs:
            t = mnq.ts[i]
            m = mes_c.get(t)
            if m is not None and m > 0 and mnq.c[i] > 0:
                rows.append((t, m, mnq.c[i]))
        if len(rows) < MIN_SESSION_BARS:
            continue
        rows.sort(key=lambda r: r[0])
        out.append({
            "date": day,
            "mes": np.array([r[1] for r in rows]),
            "mnq": np.array([r[2] for r in rows]),
        })
    return out


def spread_increments(sess):
    """Per-session spread increment s_t = logret(MNQ) - logret(MES).
    Returns list of 1-D arrays (one per session, length = bars-1)."""
    out = []
    for s in sess:
        r_mes = np.diff(np.log(s["mes"]))
        r_mnq = np.diff(np.log(s["mnq"]))
        out.append(r_mnq - r_mes)
    return out


# ── 1. Variance ratio (session-aware, Lo-MacKinlay heteroskedastic-robust) ──
def variance_ratio(seglist, q):
    all_x = np.concatenate(seglist)
    n = all_x.size
    mu = all_x.mean()
    dev = all_x - mu
    var1 = np.mean(dev ** 2)
    # q-period overlapping sums that stay within a single session
    qsum_sq = []
    for seg in seglist:
        if seg.size < q:
            continue
        d = seg - mu
        csum = np.cumsum(d)
        # window sum_{i=t-q+1..t} = csum[t] - csum[t-q]
        w = csum[q - 1:].copy()
        w[1:] -= csum[:-q]
        qsum_sq.append(w ** 2)
    if not qsum_sq:
        return float("nan"), float("nan"), float("nan")
    varq = np.concatenate(qsum_sq).mean() / q
    vr = varq / var1
    # heteroskedasticity-robust variance of VR (Lo-MacKinlay 1988, eq. for VR*)
    theta = 0.0
    denom = (dev ** 2).sum() ** 2
    for j in range(1, q):
        # delta_j on within-session lagged pairs only
        num = 0.0
        for seg in seglist:
            if seg.size <= j:
                continue
            dseg = seg - mu
            num += np.sum((dseg[j:] ** 2) * (dseg[:-j] ** 2))
        delta_j = num / denom
        theta += ((2.0 * (q - j) / q) ** 2) * delta_j
    z = (vr - 1.0) / math.sqrt(theta) if theta > 0 else float("nan")
    return vr, z, theta


def block_bootstrap_vr_ci(seglist, q, n_boot=500, seed=SEED):
    """Session-level bootstrap CI for VR(q): resample whole sessions."""
    rng = np.random.default_rng(seed)
    segs = [s for s in seglist if s.size >= q]
    k = len(segs)
    vrs = []
    for _ in range(n_boot):
        pick = rng.integers(0, k, size=k)
        vr, _, _ = variance_ratio([segs[p] for p in pick], q)
        if vr == vr:
            vrs.append(vr)
    vrs = np.array(vrs)
    return float(np.percentile(vrs, 2.5)), float(np.percentile(vrs, 97.5))


# ── 2. OU half-life of the divergence level ──
def ou_half_life(sess):
    D_lag, dD = [], []
    for s in sess:
        r_mes = np.diff(np.log(s["mes"]))
        r_mnq = np.diff(np.log(s["mnq"]))
        incr = r_mnq - r_mes
        D = np.concatenate([[0.0], np.cumsum(incr)])  # D_0=0 at open
        # within-session pairs (D_{t-1}, D_t): dD_t = a + b*D_{t-1} + e
        D_lag.append(D[:-1])
        dD.append(np.diff(D))
    x = np.concatenate(D_lag)
    y = np.concatenate(dD)
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    b = beta[1]
    rho = 1.0 + b
    if rho <= 0 or rho >= 1:
        hl = float("inf") if rho >= 1 else 0.0
    else:
        hl = -math.log(2.0) / math.log(rho) * BAR_MIN
    return b, rho, hl


# ── 3. Autocorrelation of spread increments ──
def autocorr(seglist, maxlag=12):
    all_x = np.concatenate(seglist)
    x = all_x - all_x.mean()
    denom = np.sum(x * x)
    out = []
    for lag in range(1, maxlag + 1):
        num = 0.0
        for seg in seglist:
            d = seg - all_x.mean()
            if d.size > lag:
                num += np.sum(d[lag:] * d[:-lag])
        out.append(num / denom)
    return out


# ── 5. Indicative payoff of the literal rule ──
def rule_payoff(sess):
    """Fade z>=2, exit at first of |z|<=0.5 / +12 bars / EOD.
    1 MES + 1 MNQ (~dollar-neutral micro pair). Friction assumed retail."""
    MES_PT, MNQ_PT = ds.SPECS["MES"]["pt"], ds.SPECS["MNQ"]["pt"]
    MES_TICK_USD = ds.SPECS["MES"]["tick"] * MES_PT   # $1.25
    MNQ_TICK_USD = ds.SPECS["MNQ"]["tick"] * MNQ_PT   # $0.50
    COMM = ds.SPECS["MES"]["comm_rt"] + ds.SPECS["MNQ"]["comm_rt"]  # round-turn pair
    SLIP = 2 * (MES_TICK_USD + MNQ_TICK_USD)          # 1 tick/leg * 2 sides
    FRICTION = COMM + SLIP
    trades = []
    warmup_blocked = 0
    signals_total = 0
    for s in sess:
        mes, mnq = s["mes"], s["mnq"]
        r = np.log(mnq / mnq[0]) - np.log(mes / mes[0])  # D_t vs open
        n = r.size
        i = Z_WINDOW  # need a full trailing window (spec: reset fresh each session)
        # count how many bars are lost to warm-up
        warmup_blocked += min(Z_WINDOW, n)
        while i < n - 1:
            win = r[i - Z_WINDOW:i]
            sd = win.std(ddof=1)
            if sd <= 0:
                i += 1
                continue
            z = (r[i] - win.mean()) / sd
            if abs(z) >= Z_ENTRY:
                signals_total += 1
                side = -1 if r[i] > win.mean() else 1  # fade: short spread if rich
                # walk forward to exit
                j = i + 1
                exit_j = min(n - 1, i + HORIZON_BARS)
                while j <= exit_j:
                    wj = r[max(0, j - Z_WINDOW):j]
                    if wj.size >= 5 and wj.std(ddof=1) > 0:
                        zj = (r[j] - wj.mean()) / wj.std(ddof=1)
                        if abs(zj) <= Z_EXIT:
                            exit_j = j
                            break
                    j += 1
                # leg PnL: side=+1 means long spread = long MNQ, short MES
                d_mnq = (mnq[exit_j] - mnq[i]) * MNQ_PT
                d_mes = (mes[exit_j] - mes[i]) * MES_PT
                pnl = side * (d_mnq - d_mes) - FRICTION
                trades.append(pnl)
                i = exit_j + 1  # no overlapping positions
            else:
                i += 1
    trades = np.array(trades) if trades else np.array([0.0])
    return {
        "friction_per_trade": FRICTION,
        "n_trades": int(len(trades)) if trades.size and trades[0] != 0 or len(trades) > 1 else len(trades),
        "signals_total": signals_total,
        "mean_pnl": float(trades.mean()),
        "median_pnl": float(np.median(trades)),
        "win_rate": float((trades > 0).mean()),
        "total_pnl": float(trades.sum()),
        "sharpe_per_trade": float(trades.mean() / trades.std(ddof=1)) if trades.size > 2 and trades.std() > 0 else float("nan"),
        "warmup_bars_blocked": warmup_blocked,
    }


def era_label(d):
    y = d.year
    if y <= 2021:
        return "2019-2021"
    if y <= 2023:
        return "2022-2023"
    return "2024-2026"


def main():
    print("Loading MES + MNQ 5-min RTH bars and aligning by timestamp...")
    sess = build_sessions()
    inc = spread_increments(sess)
    all_inc = np.concatenate(inc)
    # sanity: return correlation MES vs MNQ
    r_mes = np.concatenate([np.diff(np.log(s["mes"])) for s in sess])
    r_mnq = np.concatenate([np.diff(np.log(s["mnq"])) for s in sess])
    corr = float(np.corrcoef(r_mes, r_mnq)[0, 1])

    print(f"\nSessions: {len(sess)}  ({sess[0]['date']} -> {sess[-1]['date']})")
    print(f"Spread-increment obs: {all_inc.size:,}")
    print(f"5-min return corr(MES, MNQ): {corr:.3f}  "
          f"(spread daily-vol proxy: {all_inc.std()*math.sqrt(78)*1e4:.1f} bps)")

    print("\n=== 1. VARIANCE RATIO of spread increments s_t  (VR<1 revert / >1 trend) ===")
    print(f"{'q(min)':>8} {'VR':>8} {'z*':>8} {'p':>10}   95% CI")
    for q in (2, 3, 6, 12, 24):
        vr, z, _ = variance_ratio(inc, q)
        lo, hi = block_bootstrap_vr_ci(inc, q)
        print(f"{q*BAR_MIN:>8} {vr:>8.3f} {z:>8.2f} {two_sided_p(z):>10.2e}   "
              f"[{lo:.3f}, {hi:.3f}]")

    print("\n=== 2. OU / AR(1) HALF-LIFE of divergence level D_t ===")
    b, rho, hl = ou_half_life(sess)
    print(f"b (mean-revert coef) = {b:.5f}   rho = {rho:.5f}")
    if math.isinf(hl):
        print("half-life = INF (rho>=1: random walk / trending, no reversion)")
    else:
        print(f"half-life = {hl:.1f} min  ({hl/BAR_MIN:.1f} bars)")

    print("\n=== 3. AUTOCORRELATION of s_t (lags 1..12) ===")
    ac = autocorr(inc, 12)
    print("  " + "  ".join(f"L{k+1}:{v:+.3f}" for k, v in enumerate(ac)))
    print(f"  sum(lag1..12) = {sum(ac):+.3f}   (negative => net reversion)")

    print("\n=== 4. VARIANCE RATIO VR(12=60min) BY ERA (decay check) ===")
    by_era = defaultdict(list)
    for s, seg in zip(sess, inc):
        by_era[era_label(s["date"])].append(seg)
    for era in ("2019-2021", "2022-2023", "2024-2026"):
        if by_era[era]:
            vr, z, _ = variance_ratio(by_era[era], 12)
            print(f"  {era}: VR(60min)={vr:.3f}  z*={z:.2f}  "
                  f"(n_sessions={len(by_era[era])})")

    print("\n=== 5. INDICATIVE PAYOFF of the literal rule (1 MES + 1 MNQ, net friction) ===")
    p = rule_payoff(sess)
    print(f"  assumed friction/trade: ${p['friction_per_trade']:.2f}  "
          f"(comm + 1 tick/leg/side)")
    print(f"  signals: {p['signals_total']}   trades taken: {p['n_trades']}")
    print(f"  mean PnL/trade:   ${p['mean_pnl']:+.2f}")
    print(f"  median PnL/trade: ${p['median_pnl']:+.2f}")
    print(f"  win rate:         {p['win_rate']*100:.1f}%")
    print(f"  per-trade Sharpe: {p['sharpe_per_trade']:.3f}")
    print(f"  total PnL:        ${p['total_pnl']:+,.0f}")
    print(f"  bars lost to 30-bar morning warm-up: {p['warmup_bars_blocked']:,} "
          f"(signal is dark ~09:30-12:00 ET every session)")

    print("\nDONE.")


if __name__ == "__main__":
    main()
