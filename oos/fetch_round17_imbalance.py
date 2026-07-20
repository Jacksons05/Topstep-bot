"""Round 17 cost-check — NYSE + Nasdaq closing-auction imbalance (Databento).

See oos/HYPOTHESES.md "Round 17" (DATA-BLOCKED as of the 2026-07-20 update):
before spending anything, get the EXACT cost from Historical.metadata.
get_cost() for the full-market `imbalance` schema on XNYS.PILLAR (NYSE) and
XNAS.ITCH (Nasdaq). Metadata calls are FREE — this script only prices the
pull; it does not download or spend anything (same discipline as
fetch_round20_mbo.py's confirmed-then-decide pattern).

The registered signal needs the FULL market's closing-auction imbalance, not
one symbol (net equity MOC imbalance, NYSE + Nasdaq summed, $-notional), so
this uses `symbols="ALL_SYMBOLS"` (raw_symbol) rather than a parent/
continuous selector — there's no single underlying to group equities by for
this schema, unlike ES.FUT on GLBX.MDP3.

Usage:
    .venv/bin/python oos/fetch_round17_imbalance.py

Prints cost estimates for a few candidate window sizes and does NOT download
or write any data — this is purely the disclosed-$ decision step named in
HYPOTHESES.md. The download + daily NYSE+Nasdaq aggregation pipeline (to
build the moc_imbalance.csv the frozen spec / round17_moc_drift.py consumes)
is a separate follow-up, written only once a window is approved.

NOTE: written against Databento's public API docs (2026-07) with no
DATABENTO_API_KEY available to test against — verify the first run's output
carefully; metadata.get_cost/get_dataset_range are read-only and free
regardless, so a wrong param name just fails loudly, it can't overspend.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATASETS = [
    ("XNYS.PILLAR", "NYSE Integrated (closing imbalance)"),
    ("XNAS.ITCH", "Nasdaq TotalView-ITCH (NOII)"),
]
SCHEMA = "imbalance"
# Round 17's top-tercile gate admits ~1/3 of sessions, so n>=200 post-gate
# trades needs ~600+ trading sessions (~2.4 years) at minimum. Priced across
# a few window sizes so the account holder can see how cost scales with
# history length and pick against budget — none of this downloads anything.
CANDIDATE_YEARS = [1, 3, 5]


def _dataset_bounds(client, dataset: str):
    """Return (start_date_str, end_date_str) the account is actually
    licensed for, or None if the lookup fails (candidate windows then fall
    back to today's date as the upper bound)."""
    try:
        rng = client.metadata.get_dataset_range(dataset=dataset)
    except Exception as exc:  # noqa: BLE001
        print(f"  get_dataset_range failed: {exc}")
        return None
    try:
        start = str(rng["start"])[:10]
        end = str(rng["end"])[:10]
    except Exception:  # noqa: BLE001 - unknown return shape, show it raw
        print(f"  get_dataset_range returned an unexpected shape: {rng!r}")
        return None
    print(f"  licensed range: {start} .. {end}")
    return start, end


def main() -> int:
    key = os.environ.get("DATABENTO_API_KEY", "")
    if not key:
        print(
            "DATABENTO_API_KEY missing from .env — add it (Cursor Cloud: "
            "Dashboard > Cloud Agents > Secrets), then rerun. "
            "metadata.get_cost()/get_dataset_range() are FREE and read-only "
            "(nothing is downloaded by this script), so this is safe to run "
            "the moment the key exists.",
            file=sys.stderr,
        )
        return 1

    import databento as db
    client = db.Historical(key)
    now = datetime.now(timezone.utc)

    total_5y = 0.0
    for dataset, label in DATASETS:
        print("=" * 70)
        print(f"{label}  [{dataset}] schema={SCHEMA}")
        bounds = _dataset_bounds(client, dataset)
        avail_end = bounds[1] if bounds else now.date().isoformat()

        for yrs in CANDIDATE_YEARS:
            start = (now - timedelta(days=365 * yrs)).date().isoformat()
            if bounds and start < bounds[0]:
                start = bounds[0]
            try:
                cost = client.metadata.get_cost(
                    dataset=dataset, symbols="ALL_SYMBOLS", schema=SCHEMA,
                    start=start, end=avail_end,
                )
                print(f"  {yrs}y ({start}..{avail_end}): ${cost:,.2f}")
                if yrs == 5:
                    total_5y += cost
            except Exception as exc:  # noqa: BLE001
                print(f"  {yrs}y ({start}..{avail_end}): FAILED -- {exc}")

    print("=" * 70)
    print(f"Combined NYSE+Nasdaq, 5y candidate window: ${total_5y:,.2f} "
          "(sum of each dataset's 5y row above; 0.00 if either call failed)")
    print(
        "Estimate only — nothing downloaded, nothing spent. Report these "
        "numbers back before deciding on a window; the download + daily "
        "NYSE+Nasdaq aggregation pipeline (oos/HYPOTHESES.md Round 17's "
        "moc_imbalance.csv contract: date, imbalance_usd, es_1550, es_1558) "
        "is a separate follow-up, only written once a window is approved."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
