"""Fetch FREE daily continuous futures (Yahoo chart API, no key) to a local cache.

One-time (or refresh) download so research runs OFFLINE and deterministic afterward.
Saves one CSV per symbol to oos/data/free_daily/<root>.csv  (date,open,high,low,close,volume).

  .venv/bin/python oos/free_daily_fetch.py            # fetch all
  .venv/bin/python oos/free_daily_fetch.py --report   # what's cached
"""
from __future__ import annotations

import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "free_daily"
UA = {"User-Agent": "Mozilla/5.0 (research; personal use)"}
# root -> yahoo symbol (continuous front-month)
SYMS = {"CL": "CL=F", "NG": "NG=F", "GC": "GC=F", "HG": "HG=F", "SI": "SI=F",
        "PL": "PL=F", "ZW": "ZW=F", "ZC": "ZC=F", "ZS": "ZS=F", "ZL": "ZL=F",
        "ZM": "ZM=F", "HE": "HE=F", "LE": "LE=F", "ES": "ES=F", "NQ": "NQ=F",
        "ZN": "ZN=F", "6E": "6E=F"}


def fetch(client: httpx.Client, ysym: str) -> list[dict]:
    r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
                   params={"range": "30y", "interval": "1d"})
    r.raise_for_status()
    res = (r.json().get("chart", {}).get("result") or [None])[0]
    if not res:
        return []
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        c = (q.get("close") or [None])[i]
        if c is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(),
            "open": (q.get("open") or [None])[i] or c,
            "high": (q.get("high") or [None])[i] or c,
            "low": (q.get("low") or [None])[i] or c,
            "close": c,
            "volume": (q.get("volume") or [0])[i] or 0,
        })
    # de-dup dates (yahoo occasionally repeats the live bar)
    seen, out = set(), []
    for row in rows:
        if row["date"] in seen:
            out[-1] = row
            continue
        seen.add(row["date"]); out.append(row)
    return out


def report() -> int:
    if not OUT.exists():
        print("no cache yet"); return 0
    for f in sorted(OUT.glob("*.csv")):
        with f.open() as fh:
            rows = list(csv.DictReader(fh))
        print(f"  {f.stem:<4} {len(rows):>6} rows  {rows[0]['date']} .. {rows[-1]['date']}")
    return 0


def main() -> int:
    if "--report" in sys.argv:
        return report()
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=30, headers=UA) as client:
        for root, ysym in SYMS.items():
            try:
                rows = fetch(client, ysym)
            except Exception as e:  # noqa: BLE001
                print(f"  {root}: FAILED {e}"); continue
            if len(rows) < 1000:
                print(f"  {root}: only {len(rows)} rows -- skip write"); continue
            with (OUT / f"{root}.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
                w.writeheader(); w.writerows(rows)
            print(f"  {root}: {len(rows)} rows  {rows[0]['date']} .. {rows[-1]['date']}")
            time.sleep(0.4)  # be polite
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
