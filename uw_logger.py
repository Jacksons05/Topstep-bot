"""Unusual Whales signal-quality logger + offline analyzer.

Behavior-neutral instrumentation: it records what UW said and what price did
next, so you can finally answer "does the UW flow lean actually predict ES/NQ?"
before trusting the 30%-weight blend in engine.py. It never touches an order.

Two halves:

  1. UWFlowLogger — appended to by the engine each cycle. One CSV row per
     evaluated symbol: ts, symbol, spot, uw_lean, quant_lean. Fail-open: a
     logging error must never disturb the trading loop. Enabled by setting
     UW_FLOW_LOG=<path> (empty = off).

  2. Offline analyzer (`python uw_logger.py [path] [horizon_sec]`) — joins each
     row to the first later row (same symbol) at least `horizon_sec` ahead,
     computes the forward return, and reports how well uw_lean predicted it,
     with the raw quant_lean as a baseline for comparison:
        * N observations
        * Pearson correlation(signal, forward_return)
        * directional hit-rate (sign agreement)
     If UW's correlation/hit-rate doesn't beat the quant baseline, the blend is
     not earning its weight.
"""
from __future__ import annotations

import sys
import time

CSV_HEADER = "ts,symbol,spot,uw_lean,quant_lean\n"


class UWFlowLogger:
    """Append-only CSV recorder for UW signal-quality analysis. Fail-open."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None
        if not path:
            return
        try:
            import os
            new = not os.path.exists(path) or os.path.getsize(path) == 0
            self._fh = open(path, "a", buffering=1)  # line-buffered
            if new:
                self._fh.write(CSV_HEADER)
        except Exception:  # noqa: BLE001 - never break the engine over logging
            self._fh = None

    @property
    def enabled(self) -> bool:
        return self._fh is not None

    def log(self, symbol: str, spot: float, uw_lean: float,
            quant_lean: float, ts: float | None = None) -> None:
        if self._fh is None:
            return
        try:
            t = time.time() if ts is None else ts
            self._fh.write(f"{t:.3f},{symbol},{spot:.6g},{uw_lean:+.4f},{quant_lean:+.4f}\n")
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


# ── offline analysis ─────────────────────────────────────────────────────────

def _read_rows(path: str) -> list[dict]:
    import csv
    rows: list[dict] = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "ts": float(r["ts"]),
                    "symbol": r["symbol"],
                    "spot": float(r["spot"]),
                    "uw_lean": float(r["uw_lean"]),
                    "quant_lean": float(r["quant_lean"]),
                })
            except (KeyError, ValueError):
                continue
    return rows


def _forward_pairs(rows: list[dict], horizon_sec: float):
    """For each row, find the first same-symbol row ≥ horizon_sec later and
    return aligned lists (uw_lean, quant_lean, forward_return)."""
    by_sym: dict[str, list[dict]] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)

    uw, qn, fwd = [], [], []
    for sym, rs in by_sym.items():
        rs.sort(key=lambda x: x["ts"])
        j = 0
        for i, r in enumerate(rs):
            target = r["ts"] + horizon_sec
            if j <= i:
                j = i + 1
            while j < len(rs) and rs[j]["ts"] < target:
                j += 1
            if j >= len(rs):
                break
            entry, exit_ = r["spot"], rs[j]["spot"]
            if entry <= 0:
                continue
            uw.append(r["uw_lean"])
            qn.append(r["quant_lean"])
            fwd.append(exit_ / entry - 1.0)
    return uw, qn, fwd


def _report(name: str, signal, fwd) -> str:
    import numpy as np
    s = np.asarray(signal, dtype=float)
    f = np.asarray(fwd, dtype=float)
    n = len(s)
    if n < 2 or s.std() == 0 or f.std() == 0:
        return f"  {name:11} n={n:<5} corr=   n/a  hit_rate=   n/a  (insufficient variance)"
    corr = float(np.corrcoef(s, f)[0, 1])
    # directional hit-rate over rows where the signal took a side
    mask = s != 0
    hits = (np.sign(s[mask]) == np.sign(f[mask]))
    hit_rate = float(hits.mean()) if mask.any() else float("nan")
    return (f"  {name:11} n={n:<5} corr={corr:+.3f}  "
            f"hit_rate={hit_rate:5.1%}  (over {int(mask.sum())} directional rows)")


def analyze(path: str, horizon_sec: float) -> int:
    try:
        rows = _read_rows(path)
    except FileNotFoundError:
        print(f"✗ no log at {path} — set UW_FLOW_LOG={path} and let the engine run.")
        return 1
    if len(rows) < 2:
        print(f"only {len(rows)} row(s) in {path} — not enough to analyze yet.")
        return 1
    uw, qn, fwd = _forward_pairs(rows, horizon_sec)
    span_min = (rows[-1]["ts"] - rows[0]["ts"]) / 60.0
    print(f"\nUW signal-quality report — {path}")
    print(f"  {len(rows)} rows over {span_min:.0f} min | forward horizon = {horizon_sec:g}s | "
          f"{len(fwd)} matched pairs\n")
    print(_report("UW lean", uw, fwd))
    print(_report("quant lean", qn, fwd))
    print("\n  Read: if UW lean's corr/hit-rate doesn't beat the quant baseline,")
    print("  the 30%-weight blend in engine.py is not earning its weight.\n")
    return 0


def main() -> int:
    from config import CONFIG
    path = sys.argv[1] if len(sys.argv) > 1 else (CONFIG.uw_flow_log or "uw_flow_log.csv")
    horizon = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0
    return analyze(path, horizon)


if __name__ == "__main__":
    raise SystemExit(main())
