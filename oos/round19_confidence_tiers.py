"""Round 19 runner — quant-signal confidence stratification.

See oos/HYPOTHESES.md "Round 19" for the full frozen spec, rationale, and PASS
bar. Reuses backtest_fast._quant_arrays + _simulate and backtest_oos's own
data loader / stats helpers verbatim (tstat_p, boot_p, cell — RNG_SEED=7,
BOOT_N=20,000, same as every prior round). The only new logic here is
partitioning trades by their own entry-bar strength value (0.5 vs 1.0)
instead of gating at a single fixed CONFIDENCE_THRESHOLD.

_quant_arrays' strength output is discrete — {0.0, 0.5, 1.0} — so a single
_simulate pass at min_strength=0.0 already admits every non-flat signal
(direction==0 bars, i.e. strength==0.0, are filtered out inside _simulate
regardless of min_strength, since there's no direction to trade). Tagging
each returned trade with the strength that gated its entry and splitting
after the fact avoids three separate _simulate passes and guarantees PARTIAL
and FULL are drawn from the exact same run.

Usage:
    .venv/bin/python oos/round19_confidence_tiers.py
    .venv/bin/python oos/round19_confidence_tiers.py --symbols ES,MNQ

Not yet run as of registration — data is already local (oos/data/{ES,MNQ}_5min.csv),
no new pull required.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
OOS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(OOS_DIR))

from backtest_fast import _quant_arrays, _simulate  # noqa: E402
from config import CONFIG  # noqa: E402
from backtest_oos import (  # noqa: E402
    SPECS, MAX_HOLD, JUDGE_SLIP_TICKS, is_rth, load, cell,
)

TIERS = {"PARTIAL": 0.5, "FULL": 1.0}
# ES is the judged instrument (full history, statistical power); MNQ is
# exploratory only (actual Topstep target instrument) — same convention as
# every prior round in this file.
JUDGED_INSTRUMENT = "ES"
JUDGED_TIER = "PARTIAL"
JUDGED_SESSION = "RTH"


def _all_trades(sym: str) -> list[dict]:
    """One _simulate pass at min_strength=0.0 (admits every non-flat signal),
    each trade tagged with the strength value that gated its own entry."""
    ts, o, h, l, c = load(sym)
    direction, strength, atr_arr, _, _ = _quant_arrays(c, h, l)
    blocked = np.zeros(len(c), dtype=np.int8)
    e_idx, x_idx, e_px, x_px, side, reason = _simulate(
        c, h, l, direction, strength, atr_arr, blocked,
        float(CONFIG.atr_stop_mult), float(CONFIG.stop_loss_pct),
        float(CONFIG.atr_target_mult), float(CONFIG.take_profit_pct),
        0.0, MAX_HOLD,
    )
    spec = SPECS[sym]
    cost = spec["comm_rt"] + 2 * JUDGE_SLIP_TICKS * spec["tick"] * spec["pt"]
    trades = []
    for i in range(len(e_idx)):
        a = int(e_idx[i])
        # _simulate gates entry i on direction[i-1]/strength[i-1] and fills at
        # closes[i] (e_idx[k] = i+1 in _simulate's own loop var) — the strength
        # that produced this trade is one bar before its recorded entry index.
        entry_strength = float(strength[a - 1])
        pts = (x_px[i] - e_px[i]) * side[i]
        trades.append({
            "entry_strength": entry_strength,
            "year": ts[a].year,
            "session": "RTH" if is_rth(ts[a]) else "overnight",
            "net_usd": float(pts * spec["pt"] - cost),
        })
    return trades


def passes(c: dict) -> bool:
    return bool(c.get("n", 0) >= 200 and (c.get("pf") or 0) >= 1.15
                and c.get("p_one_sided") is not None and c["p_one_sided"] < 0.05
                and c.get("p_bootstrap") is not None and c["p_bootstrap"] < 0.05
                and c.get("pct_years_positive", 0) >= 60)


def run_symbol(sym: str) -> dict:
    all_trades = _all_trades(sym)
    out = {}
    for tier_name, tier_strength in TIERS.items():
        tier_trades = [t for t in all_trades if t["entry_strength"] == tier_strength]
        for session in ("RTH", "overnight", "all"):
            sess_trades = tier_trades if session == "all" else [
                t for t in tier_trades if t["session"] == session
            ]
            key = f"{tier_name}_{session}"
            out[key] = cell(sess_trades)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="ES,MNQ")
    args = ap.parse_args(argv)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    missing = [s for s in symbols if not (OOS_DIR / "data" / f"{s}_5min.csv").exists()]
    if missing:
        print(f"missing data files for {missing} — run fetch_databento.py first")
        return 1

    results = {"judged": f"{JUDGED_INSTRUMENT}/{JUDGED_TIER}/{JUDGED_SESSION}", "symbols": {}}
    for sym in symbols:
        results["symbols"][sym] = run_symbol(sym)

    out_path = OOS_DIR / "round19_results.json"
    out_path.write_text(json.dumps(results, indent=2))

    print("=" * 70)
    print("Round 19 — confidence-tier stratification")
    print("=" * 70)
    for sym in symbols:
        for key, c in results["symbols"][sym].items():
            judged = " <-- JUDGED" if (sym == JUDGED_INSTRUMENT
                                        and key == f"{JUDGED_TIER}_{JUDGED_SESSION}") else ""
            n = c.get("n", 0)
            if n == 0:
                print(f"  {sym:4} {key:20} n=0{judged}")
                continue
            verdict = "PASS" if judged and passes(c) else ("FAIL" if judged else "")
            print(f"  {sym:4} {key:20} n={n:<5} pf={c.get('pf')!s:<6} "
                  f"p_t={c.get('p_one_sided')!s:<8} p_boot={c.get('p_bootstrap')!s:<8} "
                  f"yrs+={c.get('pct_years_positive')!s:<6}{judged} {verdict}")
    print("-" * 70)
    print(f"full results: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
