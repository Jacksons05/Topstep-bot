"""#1 (full) — Build oos/data/econ_calendar.csv: scheduled macro-event dates/times ET.

FOMC decision days: authoritative, reused from round13_fomc_reversal.FOMC_DATES.
BLS/BEA/Census releases: authoritative historical PUBLICATION dates via the FRED
release-dates API (needs FRED_API_KEY in .env). All these reports release 08:30 ET.
No guessed dates — every date is a recorded release, so the calendar is reproducible.

columns: date, time_et, event, source

Usage: .venv/bin/python oos/build_econ_calendar.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(HERE))
from round13_fomc_reversal import FOMC_DATES  # authoritative FOMC decision days

OUT = HERE / "data" / "econ_calendar.csv"
BASE = "https://api.stlouisfed.org/fred"
START, END = "2010-01-01", "2026-12-31"

# distinctive name substring -> (event tag, release time ET). All 08:30 ET reports.
TARGETS = [
    ("Employment Situation", "NFP", "08:30"),
    ("Consumer Price Index", "CPI", "08:30"),
    ("Unemployment Insurance Weekly Claims", "JOBLESS_CLAIMS", "08:30"),
    ("Personal Income and Outlays", "PCE", "08:30"),
    ("Advance Monthly Sales for Retail", "RETAIL_SALES", "08:30"),
    ("Gross Domestic Product", "GDP", "08:30"),
]


def get(client, path, **params):
    params.update(api_key=os.environ["FRED_API_KEY"], file_type="json")
    for attempt in (1, 2, 3):
        try:
            r = client.get(f"{BASE}/{path}", params=params, timeout=30.0)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if attempt == 3:
                raise
            time.sleep(1.5 * attempt)


def main() -> int:
    if not os.environ.get("FRED_API_KEY"):
        print("FRED_API_KEY missing from .env", file=sys.stderr)
        return 1
    rows = []  # (date, time_et, event, source)

    with httpx.Client(follow_redirects=True,
                      headers={"User-Agent": "topstep-research/1.0"}) as client:
        releases = get(client, "releases", limit=1000)["releases"]
        for substr, tag, t in TARGETS:
            cand = [r for r in releases if substr.lower() in r["name"].lower()]
            if not cand:
                print(f"WARN: no FRED release matches '{substr}' — skipped", file=sys.stderr)
                continue
            rel = min(cand, key=lambda r: len(r["name"]))  # tightest match
            data = get(client, "release/dates", release_id=rel["id"],
                       realtime_start=START, realtime_end=END, limit=10000,
                       sort_order="asc", include_release_dates_with_no_data="false")
            dts = sorted({d["date"] for d in data.get("release_dates", [])
                          if START <= d["date"] <= END})
            for d in dts:
                rows.append((d, t, tag, f"FRED/{rel['id']}"))
            print(f"{tag:<14} <- '{rel['name']}' (rid {rel['id']}): {len(dts)} dates "
                  f"{dts[0]}..{dts[-1]}")

    for d in FOMC_DATES:
        rows.append((d, "14:00", "FOMC", "federalreserve.gov"))
    print(f"FOMC           <- round13 archive: {len(FOMC_DATES)} dates")

    rows.sort()
    OUT.write_text("date,time_et,event,source\n"
                   + "\n".join(",".join(r) for r in rows) + "\n")
    n_days = len({r[0] for r in rows})
    print(f"\nwrote {len(rows)} event-rows on {n_days} distinct days -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
