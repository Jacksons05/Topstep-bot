"""FORK-C intraday dealer-GEX capture -- FREE / open-source only (CBOE delayed chain).

Snapshots the SPX options surface from CBOE's public delayed-quote JSON (greeks + OI
provided, no subscription) and appends a per-snapshot GEX reading to a local data lake.
This is the ONLY $0 route to the mechanism-correct INTRADAY 0DTE dealer gamma -- UW/vendors
retain no history, so we accumulate our own FORWARD (irrecoverable if not captured).

DATA LAKE, not a feature store: we store the raw convention-flexible components
(call/put gamma-weighted OI, spot, 0DTE subset) so any dealer-positioning convention can
be applied downstream. Run ~every 30 min during RTH.

  net_gex (SqueezeMetrics-ish) = (Sum_call gamma*OI - Sum_put gamma*OI) * 100 * spot^2 * 0.01

Usage:  .venv/bin/python forkc/gex_capture.py            # one snapshot
        .venv/bin/python forkc/gex_capture.py --report   # summary of the lake
"""
from __future__ import annotations

import csv
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent
LAKE = HERE / "data" / "spx_gex_intraday.csv"
# _SPX.json already includes SPXW/0DTE strikes (verified: ~496 same-day options in it);
# the separate _SPXW endpoint 403s, so one URL covers everything.
URLS = ["https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json"]
FIELDS = ["ts_et", "spot", "n_opts", "tot_oi", "call_goi", "put_goi", "net_gex_usd",
          "n_0dte", "call_goi_0dte", "put_goi_0dte", "net_gex_0dte_usd", "src"]
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _snapshot(client, url):
    r = client.get(url, timeout=40.0, headers={"User-Agent": "research/1.0"})
    r.raise_for_status()
    d = r.json().get("data", {})
    spot = float(d.get("current_price") or d.get("close") or 0.0)
    opts = d.get("options") or []
    return spot, opts


def capture():
    LAKE.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ET).date()
    spot, opts, srcs = 0.0, [], []
    with httpx.Client(follow_redirects=True) as client:
        for url in URLS:
            try:
                s, o = _snapshot(client, url)
                if o:
                    spot = spot or s
                    opts += o
                    srcs.append(url.rsplit("/", 1)[-1].replace(".json", ""))
            except Exception as e:  # noqa: BLE001
                print(f"[gex] {url} failed: {e}", file=sys.stderr)
    if not opts or spot <= 0:
        print("[gex] no data -- skip"); return 1

    call_goi = put_goi = 0.0
    c0 = p0 = 0.0
    n0 = 0
    tot_oi = 0.0
    for op in opts:
        m = OCC.match(str(op.get("option", "")))
        if not m:
            continue
        _root, ymd, cp, k8 = m.groups()
        try:
            exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6]))
        except ValueError:
            continue
        g = float(op.get("gamma") or 0.0)
        oi = float(op.get("open_interest") or 0.0)
        tot_oi += oi
        goi = g * oi
        is0 = exp == today
        if cp == "C":
            call_goi += goi
            if is0:
                c0 += goi; n0 += 1
        else:
            put_goi += goi
            if is0:
                p0 += goi; n0 += 1

    mult = 100 * spot * spot * 0.01
    net = (call_goi - put_goi) * mult
    net0 = (c0 - p0) * mult
    row = {"ts_et": datetime.now(ET).isoformat(timespec="minutes"),
           "spot": f"{spot:.2f}", "n_opts": len(opts), "tot_oi": f"{tot_oi:.0f}",
           "call_goi": f"{call_goi:.4f}", "put_goi": f"{put_goi:.4f}",
           "net_gex_usd": f"{net:.0f}", "n_0dte": n0,
           "call_goi_0dte": f"{c0:.4f}", "put_goi_0dte": f"{p0:.4f}",
           "net_gex_0dte_usd": f"{net0:.0f}", "src": "+".join(srcs)}
    exists = LAKE.exists()
    with LAKE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)
    print(f"[gex] {row['ts_et']} spot={spot:.0f} net_gex=${net/1e9:+.2f}B "
          f"0DTE net=${net0/1e9:+.2f}B (n0={n0}) opts={len(opts)} -> {LAKE.name}")
    return 0


def report():
    if not LAKE.exists():
        print("no lake yet."); return 0
    rows = list(csv.DictReader(LAKE.open()))
    print(f"gex lake: {len(rows)} snapshots  {rows[0]['ts_et']} .. {rows[-1]['ts_et']}")
    days = sorted({r["ts_et"][:10] for r in rows})
    print(f"  days={len(days)}  0DTE-bearing snapshots={sum(1 for r in rows if int(r['n_0dte'])>0)}")
    for r in rows[-3:]:
        print(f"  {r['ts_et']} spot={r['spot']} net_gex=${float(r['net_gex_usd'])/1e9:+.2f}B "
              f"0DTE=${float(r['net_gex_0dte_usd'])/1e9:+.2f}B")
    return 0


if __name__ == "__main__":
    raise SystemExit(report() if "--report" in sys.argv else capture())
