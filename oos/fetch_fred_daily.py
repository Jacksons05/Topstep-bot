"""#2 — Fetch FREE daily macro series from FRED (no API key needed) for
regime-conditioning research. Stores one CSV per series in oos/data/macro/.

Series (regime inputs the ES-only dataset lacks):
  VIXCLS      CBOE VIX (equity implied vol)
  T10Y2Y      10y-2y Treasury spread (curve slope; recession/risk regime)
  T10Y3M      10y-3m Treasury spread
  DGS10 DGS2  10y / 2y nominal yields (level)
  DTWEXBGS    Broad trade-weighted USD index (dollar regime)
  BAMLH0A0HYM2  ICE BofA US High-Yield OAS (credit-stress regime)

Daily, free, keyless via the fredgraph.csv endpoint. Reproducible: re-running
overwrites with the latest vintage (these are revised rarely for market series).

Usage: .venv/bin/python oos/fetch_fred_daily.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent / "data" / "macro"
SERIES = ["VIXCLS", "T10Y2Y", "T10Y3M", "DGS10", "DGS2", "DTWEXBGS", "BAMLH0A0HYM2"]
URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd=1990-01-01"


def fetch_one(client: httpx.Client, sid: str) -> int:
    r = client.get(URL.format(sid=sid), timeout=30.0)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    if len(lines) < 2:
        raise ValueError("empty CSV")
    # header: observation_date,<SID>  (drop it); rows: date,value ("." = missing)
    rows = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 2:
            continue
        d, v = parts[0].strip(), parts[1].strip()
        if v in ("", ".", "NaN"):
            continue
        rows.append(f"{d},{v}")
    out = OUT / f"{sid}.csv"
    out.write_text("date,value\n" + "\n".join(rows) + "\n")
    return len(rows)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ok, fail = 0, 0
    with httpx.Client(follow_redirects=True,
                      headers={"User-Agent": "topstep-research/1.0"}) as client:
        for sid in SERIES:
            for attempt in (1, 2, 3):
                try:
                    n = fetch_one(client, sid)
                    print(f"{sid}: {n} daily obs -> data/macro/{sid}.csv")
                    ok += 1
                    break
                except Exception as e:  # noqa: BLE001
                    if attempt == 3:
                        print(f"{sid}: FAILED after 3 tries ({e})", file=sys.stderr)
                        fail += 1
                    else:
                        import time
                        time.sleep(1.5 * attempt)
    print(f"\ndone: {ok} ok, {fail} failed")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
