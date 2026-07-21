"""Eval-pass Monte-Carlo for the $50K Topstep Combine (confirmed rules).

Answers the edge-independent question: given a per-trade edge and a sizing
envelope, what is P(pass the Combine) under the ACTUAL rule set?

Rules modelled (memory: topstep-50k-combine-rules):
  * Start $50,000; profit target +$3,000 (reach & maintain).
  * DLL $1,000/day, real-time (we size so a soft daily stop < $1,000 always
    triggers first -> the hard DLL is never breached by construction).
  * Trailing MLL $2,000, ratchets on END-OF-DAY balance; locks at $50,000 once
    EOD balance reaches $52,000. Real-time binding intraday (uses the day's
    running-low equity).
  * Consistency: best single day's profit < 50% of total profit at pass. A daily
    profit cap < $1,500 auto-satisfies it at the $3,000 target.

The honest point this quantifies: risk management shapes the OUTCOME
DISTRIBUTION, it cannot create drift. At zero edge P(pass) < P(fail) because the
target ($3k) is farther than the floor ($2k). This finds the edge BAR — the
expectancy a real signal must clear — and the sizing that best exploits it.

Trade model: each trade wins +rr*risk (prob p) or loses -risk (prob 1-p).
edge/trade (R) = p*rr - (1-p);  expectancy $ = risk * edge_R.
Seeded, reproducible.
"""
from __future__ import annotations

import numpy as np

START = 50_000.0
TARGET = 3_000.0
TRAIL = 2_000.0
LOCK_AT = 2_000.0        # EOD profit that locks the floor at breakeven
DLL = 1_000.0
SEED = 20260720


def simulate(p, rr, risk, trades_per_day, soft_stop, day_cap,
             max_days=40, n_paths=20_000, seed=SEED):
    """Vectorised day-structured MC. Returns outcome probabilities + timing."""
    assert soft_stop < DLL, "soft daily stop must be under the DLL"
    rng = np.random.default_rng(seed)
    n = n_paths
    balance = np.full(n, START)
    floor = np.full(n, START - TRAIL)     # 48,000
    locked = np.zeros(n, bool)
    best_day = np.zeros(n)
    alive = np.ones(n, bool)
    outcome = np.full(n, "timeout", dtype=object)
    day_of = np.full(n, max_days, int)
    rows = np.arange(n)

    for day in range(max_days):
        u = rng.random((n, trades_per_day))
        tp = np.where(u < p, rr * risk, -risk)
        cum = np.cumsum(tp, axis=1)
        # first trade that trips the soft daily stop or the profit cap
        hit = (cum <= -soft_stop) | (cum >= day_cap)
        first = np.where(hit.any(1), hit.argmax(1), trades_per_day - 1)
        day_pnl = cum[rows, first]
        day_min = np.minimum.accumulate(cum, axis=1)[rows, first]   # intraday low

        # intraday real-time MLL breach (uses running-low equity), alive only
        intraday_low = balance + day_min
        mll_breach = alive & (intraday_low <= floor)
        outcome[mll_breach] = "fail_mll"
        day_of[mll_breach] = day
        alive = alive & ~mll_breach

        # settle the day on still-alive paths
        balance = np.where(alive, balance + day_pnl, balance)
        best_day = np.where(alive, np.maximum(best_day, day_pnl), best_day)

        # EOD trailing update (only where not locked)
        upd = alive & ~locked
        floor = np.where(upd, np.maximum(floor, balance - TRAIL), floor)
        newlock = upd & (balance >= START + LOCK_AT)
        floor = np.where(newlock, START, floor)
        locked = locked | newlock

        # pass check: target reached AND consistency satisfied
        total = balance - START
        passed = alive & (total >= TARGET) & (best_day < 0.5 * total)
        outcome[passed] = "pass"
        day_of[passed] = day
        alive = alive & ~passed

        if not alive.any():
            break

    return {
        "p_pass": float((outcome == "pass").mean()),
        "p_fail_mll": float((outcome == "fail_mll").mean()),
        "p_timeout": float((outcome == "timeout").mean()),
        "median_days_to_pass": (float(np.median(day_of[outcome == "pass"]))
                                if (outcome == "pass").any() else None),
        "edge_R": round(p * rr - (1 - p), 4),
        "exp_usd_per_trade": round(risk * (p * rr - (1 - p)), 2),
    }


def edge_bar_sweep():
    print("=" * 84)
    print("  EDGE BAR — P(pass) vs win rate")
    print("  sizing: risk $150/trade, RR 1:1, 4 trades/day, soft stop -$600, "
          "profit cap $1,200")
    print("=" * 84)
    print(f"{'win%':>6}{'edge/trade':>12}{'$exp/trade':>12}{'P(pass)':>10}"
          f"{'P(fail MLL)':>13}{'P(timeout)':>12}{'med days':>10}")
    rr, risk, T, stop, cap = 1.0, 150.0, 4, 600.0, 1200.0
    for p in (0.48, 0.50, 0.52, 0.54, 0.55, 0.56, 0.58, 0.60):
        r = simulate(p, rr, risk, T, stop, cap)
        md = r["median_days_to_pass"]
        print(f"{p*100:>6.0f}{r['edge_R']:>12.3f}{r['exp_usd_per_trade']:>12.0f}"
              f"{r['p_pass']:>10.3f}{r['p_fail_mll']:>13.3f}{r['p_timeout']:>12.3f}"
              f"{(md if md is not None else 0):>10.0f}")


def envelope_sweep():
    print("\n" + "=" * 84)
    print("  RISK ENVELOPE — P(pass) at a fixed modest edge (win 55%, RR 1:1)")
    print("=" * 84)
    print(f"{'risk$':>7}{'trades/day':>12}{'softstop$':>11}{'cap$':>7}"
          f"{'P(pass)':>10}{'P(fail)':>10}{'med days':>10}")
    for risk, T, stop, cap in [
        (100, 3, 400, 1000), (100, 4, 500, 1200), (150, 4, 600, 1200),
        (150, 5, 750, 1400), (200, 3, 600, 1200), (250, 3, 700, 1400),
    ]:
        r = simulate(0.55, 1.0, float(risk), T, float(stop), float(cap))
        md = r["median_days_to_pass"]
        print(f"{risk:>7}{T:>12}{stop:>11}{cap:>7}{r['p_pass']:>10.3f}"
              f"{r['p_fail_mll']:>10.3f}{(md if md is not None else 0):>10.0f}")


if __name__ == "__main__":
    edge_bar_sweep()
    envelope_sweep()
    print("\nNote: hard DLL never breaches by construction (soft stop < $1,000).")
    print("At zero edge (win 50%, RR 1:1) P(pass) < P(fail) — target $3k is")
    print("farther than the $2k floor; risk mgmt shapes variance, not drift.")
