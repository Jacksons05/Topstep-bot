"""#1 — Build oos/data/econ_calendar.csv (scheduled macro-event dates/times ET).

FOMC decision days are authoritative (reused from round13_fomc_reversal.FOMC_DATES,
sourced from the Fed's historical archive). Schema is event-agnostic so BLS/BEA
releases (CPI, NFP, jobless claims, PCE, retail sales, GDP) can be appended later
from an authoritative source — FRED's release-dates API (needs a free FRED_API_KEY)
or a verified static list. We do NOT algorithmically guess release dates: holiday
shifts make first-Friday/mid-month heuristics wrong often enough to inject phantom
signal, which violates the offline-reproducible research rule.

columns: date, time_et, event, source

Usage: .venv/bin/python oos/build_econ_calendar.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from round13_fomc_reversal import FOMC_DATES  # authoritative FOMC decision days

OUT = HERE / "data" / "econ_calendar.csv"
FOMC_TIME_ET = "14:00"  # nominal decision-release time (pre-2013 varied 12:30-14:15)


def main() -> int:
    rows = [("date", "time_et", "event", "source")]
    for d in sorted(FOMC_DATES):
        rows.append((d, FOMC_TIME_ET, "FOMC", "federalreserve.gov"))
    OUT.write_text("\n".join(",".join(r) for r in rows) + "\n")
    print(f"wrote {len(rows)-1} events -> {OUT}")
    print(f"  FOMC: {len(FOMC_DATES)} decision days "
          f"{min(FOMC_DATES)} .. {max(FOMC_DATES)}")
    print("  NOTE: BLS/BEA releases (CPI/NFP/claims/PCE/GDP) NOT included — need "
          "a free FRED_API_KEY (release-dates API) or a verified static list to add "
          "authoritative dates. No guessed dates (would violate reproducibility).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
