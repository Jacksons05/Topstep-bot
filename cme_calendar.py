"""CME equity-index futures holiday / early-close calendar — static table.

Covers the products this bot trades (ES/NQ/MES/MNQ, CME Globex equity index).
Two kinds of special day:

  * FULL closure — no session at all (and the preceding overnight session
    halts; we simply treat the whole calendar day as closed).
  * EARLY close  — the day session halts early (typically ~13:00 ET) and
    reopens at the normal 18:00 ET Globex open.

Times are ET and deliberately CONSERVATIVE: we halt entries/flatten a margin
BEFORE the earliest plausible exchange halt rather than track the exact
product-by-product minute. Being flat 15 minutes early costs nothing; holding
into a halted session risks carrying an unmanageable position against the
trailing MLL.

⚠ UPDATE ANNUALLY from the official CME Group holiday calendar
(cmegroup.com → Trading Hours & Holiday Calendar). Verify observed dates —
exchange schedules occasionally differ from the federal holiday pattern.
`preflight.py` warns when today's year has no entries in this table.
"""
from __future__ import annotations

from datetime import date, time

# Days with NO equity-index futures session (full closure).
FULL_CLOSURES: frozenset[str] = frozenset({
    # 2026
    "2026-01-01",   # New Year's Day
    "2026-04-03",   # Good Friday
    "2026-12-25",   # Christmas Day
    # 2027 — VERIFY when CME publishes the final calendar
    "2027-01-01",   # New Year's Day
    "2027-03-26",   # Good Friday
    "2027-12-24",   # Christmas (observed — Dec 25 is a Saturday)
})

# Days when the equity-index day session halts early. Value = conservative
# halt time (ET) at/after which we treat the market as CLOSED until the
# 18:00 ET Globex reopen. CME equity-index early closes are typically
# 13:00 ET (12:00 CT); we use 13:00 and flatten a margin before it.
_EARLY_HALT = time(13, 0)
EARLY_CLOSES: dict[str, time] = {
    # 2026
    "2026-01-19": _EARLY_HALT,   # Martin Luther King Jr. Day
    "2026-02-16": _EARLY_HALT,   # Presidents' Day
    "2026-05-25": _EARLY_HALT,   # Memorial Day
    "2026-06-19": _EARLY_HALT,   # Juneteenth
    "2026-07-03": _EARLY_HALT,   # Independence Day (observed — Jul 4 is a Saturday)
    "2026-09-07": _EARLY_HALT,   # Labor Day
    "2026-11-26": _EARLY_HALT,   # Thanksgiving Day
    "2026-11-27": time(13, 15),  # Day after Thanksgiving
    "2026-12-24": time(13, 15),  # Christmas Eve
    # 2027 — VERIFY when CME publishes the final calendar
    "2027-01-18": _EARLY_HALT,   # Martin Luther King Jr. Day
    "2027-02-15": _EARLY_HALT,   # Presidents' Day
    "2027-05-31": _EARLY_HALT,   # Memorial Day
    "2027-06-18": _EARLY_HALT,   # Juneteenth (observed — Jun 19 is a Saturday)
    "2027-07-05": _EARLY_HALT,   # Independence Day (observed — Jul 4 is a Sunday)
    "2027-09-06": _EARLY_HALT,   # Labor Day
    "2027-11-25": _EARLY_HALT,   # Thanksgiving Day
    "2027-11-26": time(13, 15),  # Day after Thanksgiving
}

# Flatten this many minutes BEFORE an early-close halt.
EARLY_CLOSE_FLATTEN_MARGIN_MIN = 15

_COVERED_YEARS = frozenset(
    int(d[:4]) for d in (set(FULL_CLOSURES) | set(EARLY_CLOSES))
)


def is_full_closure(d: date) -> bool:
    """True when the exchange has no equity-index session at all on this day."""
    return d.isoformat() in FULL_CLOSURES


def early_close_halt(d: date) -> time | None:
    """The ET halt time when this is an early-close day, else None."""
    return EARLY_CLOSES.get(d.isoformat())


def year_covered(year: int) -> bool:
    """True when the static table has entries for this year — preflight warns
    when it doesn't, which is the annual-update reminder."""
    return year in _COVERED_YEARS
