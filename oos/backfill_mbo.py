"""Nightly T+1 MES MBO backfill (Databento Historical API, GLBX.MDP3).

Replaces live_capture_mbo.py. Same purpose -- build the forward/live
confirmation window Round 20's own verdict rule requires (see
oos/HYPOTHESES.md "Round 20") and accumulate future MBO-dependent research
data -- but via the Historical API instead of a live stream: the account's
plan includes L2/L3 (mbo, mbp-10) flat-rate for the trailing month, while
live MBO streaming needs a separate live license the account doesn't carry
(verified 2026-07-16: live subscribe to mbo/mbp-10 -> "Not authorized",
historical get_cost for one MES MBO day inside the window -> $0.00).

T+1 historical data is strictly better for this purpose anyway: finalized,
gap-free, and each UTC-midnight-aligned day starts with a synthetic full
order book snapshot the live stream doesn't get mid-session. The data is
still "forward" in the OOS sense -- every day pulled postdates Round 20's
registration (2026-07-16) -- only its retrieval is delayed one day.

Explicitly NOT wired into the live trading engine (same stance as the live
capture it replaces).

Each run pulls every missing completed UTC day from START_DATE through
yesterday, one file per day. Idempotent: a day whose file exists is skipped,
downloads stream to a .part file renamed only on success, so a killed run
never leaves a day looking complete. Must run at least every ~3 weeks or
days age out of the plan's flat-rate month; the per-day cost guard below
skips (never bills) anything that has.

Usage: .venv/bin/python oos/backfill_mbo.py            # normal nightly run
       .venv/bin/python oos/backfill_mbo.py --dry-run  # report, no download
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import databento as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mbo_backfill")

DATASET = "GLBX.MDP3"
SCHEMA = "mbo"
SYMBOL = "MES.v.0"
OUT_DIR = ROOT / "oos" / "data" / "live_mbo"

# Round 20 registration date -- the forward window starts here. Days before
# this are pre-registration and must NOT land in this directory (they would
# contaminate the forward-confirmation claim).
START_DATE = date(2026, 7, 16)

# A day inside the plan's flat-rate window quotes $0.00. Anything above this
# means the day aged out of the window (or the plan lapsed) -- skip it rather
# than silently bill the account.
MAX_DAY_COST_USD = 0.05


def _day_path(d: date) -> Path:
    return OUT_DIR / f"MES_mbo_{d.isoformat()}.dbn.zst"


def _missing_days(today_utc: date) -> list[date]:
    """Completed UTC days >= START_DATE not yet on disk. Skips UTC Saturdays
    (CME Globex is closed Fri 17:00 ET - Sun 18:00 ET, so a UTC Saturday
    holds no session at all; Sundays hold the 18:00 ET open and are kept)."""
    days = []
    d = START_DATE
    while d < today_utc:
        if d.isoweekday() != 6 and not _day_path(d).exists():
            days.append(d)
        d += timedelta(days=1)
    return days


def _pull_day(client: db.Historical, d: date, dry_run: bool) -> bool:
    params = dict(
        dataset=DATASET, schema=SCHEMA, symbols=[SYMBOL], stype_in="continuous",
        start=f"{d.isoformat()}T00:00", end=f"{(d + timedelta(days=1)).isoformat()}T00:00",
    )
    cost = client.metadata.get_cost(**params)
    if cost > MAX_DAY_COST_USD:
        log.warning(f"{d}: quoted ${cost:.2f} > ${MAX_DAY_COST_USD:.2f} guard "
                    "-- aged out of the plan's flat-rate month (or plan lapsed), skipping")
        return False
    if dry_run:
        log.info(f"{d}: would pull (${cost:.2f})")
        return True

    out_path = _day_path(d)
    part_path = out_path.with_suffix(out_path.suffix + ".part")
    log.info(f"{d}: pulling (${cost:.2f}) -> {out_path.name}")
    try:
        client.timeseries.get_range(**params, path=str(part_path))
        part_path.rename(out_path)
    except Exception as e:  # noqa: BLE001 -- one bad day must not block the rest
        log.error(f"{d}: pull failed: {e}")
        part_path.unlink(missing_ok=True)
        return False
    log.info(f"{d}: done ({out_path.stat().st_size / 1e6:.1f} MB)")
    return True


def main() -> int:
    key = os.environ.get("DATABENTO_API_KEY", "")
    if not key:
        log.error("DATABENTO_API_KEY missing from .env")
        return 1
    dry_run = "--dry-run" in sys.argv

    # date.today() in UTC terms: use the Historical clock implied by "completed
    # day = strictly before today (UTC)". datetime.now(UTC).date() is exact.
    from datetime import datetime, timezone
    today_utc = datetime.now(timezone.utc).date()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    days = _missing_days(today_utc)
    if not days:
        log.info("nothing to do -- all completed days since "
                 f"{START_DATE} already on disk")
        return 0

    client = db.Historical(key)
    failures = sum(0 if _pull_day(client, d, dry_run) else 1 for d in days)
    if failures:
        log.error(f"{failures}/{len(days)} day(s) failed")
        return 1
    log.info(f"all {len(days)} day(s) OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
