"""FORWARD paper-log for the overnight drift — the ONLY non-fishing way to keep
watching it (accumulates genuinely fresh out-of-sample data; the 2010-2026 holdout
is spent). Run daily after the 09:30 ET RTH open.

Each run appends the most-recent COMPLETED overnight (18:00 ET evening open ->
09:30 ET next RTH open) for MES + MNQ to oos/data/overnight_forward_log.csv, using
free account-native ProjectX historical bars + free FRED VIX. Idempotent: a
(date, sym) already logged is skipped. RAW drift (no VIX guard -- the guard was shown
counterproductive, oos/overnight_tail_guard.py).

  run:      .venv/bin/python oos/overnight_forward_log.py
  summary:  .venv/bin/python oos/overnight_forward_log.py --report
"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
LOG = ROOT / "oos" / "data" / "overnight_forward_log.csv"
ET = ZoneInfo("America/New_York")
FIELDS = ["exit_date", "sym", "px_1800", "px_0930", "drift_pts", "pnl_1ct_net", "prior_vix", "logged_at"]
SPECS = {"MES": {"pt": 5.0, "tick": 0.25, "comm_rt": 1.40},
         "MNQ": {"pt": 2.0, "tick": 0.25, "comm_rt": 1.40}}


def latest_vix() -> float:
    try:
        import httpx
        r = httpx.get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS",
                      timeout=20.0, follow_redirects=True)
        for ln in reversed(r.text.strip().splitlines()):
            v = ln.split(",")[-1].strip()
            if v not in ("", ".", "value", "VIXCLS"):
                return float(v)
    except Exception:
        pass
    return float("nan")


def _read_logged() -> set[tuple[str, str]]:
    if not LOG.exists():
        return set()
    with LOG.open() as f:
        return {(r["exit_date"], r["sym"]) for r in csv.DictReader(f)}


def _bar_price_at(bars: dict, target_et_minute: int, on_date):
    """open of the first bar on `on_date` (ET) at/after target minute-of-day."""
    times = bars.get("time") or []
    opens = bars.get("open") or []
    if len(times) != len(opens):
        return None
    for t_iso, op in zip(times, opens):
        try:
            t = datetime.fromisoformat(t_iso).astimezone(ET)
        except (ValueError, TypeError):
            continue
        if t.date() == on_date and (t.hour * 60 + t.minute) >= target_et_minute:
            return float(op)
    return None


def run():
    from projectx_executor import ProjectXBroker
    br = ProjectXBroker()
    if getattr(br, "_mock_mode", True):
        print("[fwd-log] broker in mock mode (no creds) -- cannot log")
        return 1
    vix = latest_vix()
    logged = _read_logged()
    new_rows = []
    now = datetime.now(ET)
    for sym in ("MES", "MNQ"):
        bars = br.historical_bars(sym, timeframe="5Min", limit=1200, days_back=4)
        times = bars.get("time") or []
        if not times:
            print(f"[fwd-log] {sym}: no bars/time from broker -- skip")
            continue
        # ET dates present, descending recency; find the latest date with a 09:30 bar
        et_dates = sorted({datetime.fromisoformat(t).astimezone(ET).date() for t in times})
        exit_d = None
        for d in reversed(et_dates):
            if _bar_price_at(bars, 9 * 60 + 30, d) is not None:
                exit_d = d
                break
        if exit_d is None:
            print(f"[fwd-log] {sym}: no 09:30 ET bar found -- skip")
            continue
        # evening open = 18:00 ET on the calendar day BEFORE exit_d
        prev_days = [d for d in et_dates if d < exit_d]
        px_1800 = None
        for d in reversed(prev_days):
            px_1800 = _bar_price_at(bars, 18 * 60, d)
            if px_1800 is not None:
                break
        px_0930 = _bar_price_at(bars, 9 * 60 + 30, exit_d)
        if px_1800 is None or px_0930 is None:
            print(f"[fwd-log] {sym}: missing 18:00 or 09:30 price -- skip")
            continue
        if (exit_d.isoformat(), sym) in logged:
            print(f"[fwd-log] {sym} {exit_d}: already logged -- skip")
            continue
        spec = SPECS[sym]
        cost = spec["comm_rt"] + 2 * 1 * spec["tick"] * spec["pt"]
        drift = px_0930 - px_1800
        pnl = drift * spec["pt"] - cost
        new_rows.append({"exit_date": exit_d.isoformat(), "sym": sym,
                         "px_1800": f"{px_1800:.2f}", "px_0930": f"{px_0930:.2f}",
                         "drift_pts": f"{drift:.2f}", "pnl_1ct_net": f"{pnl:.2f}",
                         "prior_vix": f"{vix:.2f}", "logged_at": now.isoformat(timespec="minutes")})
        print(f"[fwd-log] {sym} {exit_d}: 1800={px_1800:.2f} 0930={px_0930:.2f} "
              f"drift={drift:+.2f}pt pnl=${pnl:+.2f} vix={vix:.1f}")
    if new_rows:
        exists = LOG.exists()
        with LOG.open("a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if not exists:
                w.writeheader()
            w.writerows(new_rows)
        print(f"[fwd-log] appended {len(new_rows)} row(s) -> {LOG}")
    return 0


def report():
    if not LOG.exists():
        print("no forward log yet.")
        return 0
    import statistics
    rows = list(csv.DictReader(LOG.open()))
    print(f"forward overnight-drift log: {len(rows)} rows, "
          f"{rows[0]['exit_date']}..{rows[-1]['exit_date']}")
    for sym in ("MES", "MNQ"):
        p = [float(r["pnl_1ct_net"]) for r in rows if r["sym"] == sym]
        if len(p) >= 2:
            wins = sum(1 for x in p if x > 0)
            print(f"  {sym}: n={len(p)} mean=${statistics.mean(p):+.2f} "
                  f"total=${sum(p):+.0f} win={100*wins/len(p):.0f}% worst=${min(p):+.0f}")
        elif p:
            print(f"  {sym}: n={len(p)} (need >=2 for stats)")
    print("NOTE: forward OOS evidence only. Needs months to be meaningful; a stable")
    print("positive mean would revive the drift, a flat/negative one confirms decay.")
    return 0


if __name__ == "__main__":
    raise SystemExit(report() if "--report" in sys.argv else run())
