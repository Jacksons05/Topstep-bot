"""Overnight-drift strategy (scheduled, deterministic) — the one edge the research
program surfaced (oos/HYPOTHESES.md R30 + BOOKMARK-overnight-drift memory).

Config (config.py, OVERNIGHT_DRIFT_ENABLED default FALSE):
  * LONG 1x MNQ at the 18:00 ET session open (5 PM CT = the new Topstep trading day),
    exit at 06:00 ET — the EVENING slice, which is Topstep-LEGAL (flat long before the
    3:10 PM CT flatten) and did not decay post-2022.
  * $500 native stop (bracket via executor.open) -> worst night ~-$873 (under the $1k DLL).
  * Loss-streak halt: after N consecutive losing NIGHTS, sit out until a win resets.

This module holds ONLY the decision logic + persistent loss-streak/entry state — no
broker calls. engine._overnight_drift_step() wires it to execution, so the entry is a
first-class engine position (native stop + naked-guard + Topstep MLL/DLL layer apply).

SUPERSEDES the old standalone 16:00->09:30 runner (that window violated the flatten
rule; the evening slice does not). Validated IN-SAMPLE only (holdout spent) — running
on the sim/eval account is a live FORWARD test. Kill criteria in the bookmark memory.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
STATE_PATH = Path(__file__).resolve().parent / "overnight_drift_state.json"


def _hm_to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


class OvernightDrift:
    def __init__(self, cfg):
        self.symbol = cfg.overnight_drift_symbol.upper()
        self.entry_min = _hm_to_min(cfg.overnight_drift_entry_et)        # 18:00 ET -> 1080
        self.exit_min = _hm_to_min(cfg.overnight_drift_exit_et)          # 06:00 ET -> 360
        self.entry_window_min = int(cfg.overnight_drift_entry_window_min)
        self.stop_usd = float(cfg.overnight_drift_stop_usd)
        self.max_losing_nights = int(cfg.overnight_drift_max_losing_nights)
        self.contracts = int(cfg.overnight_drift_contracts)
        self._st = self._load()

    # ── persistent state ────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:  # noqa: BLE001
            return {"consecutive_losses": 0, "last_entry_session": "", "history": []}

    def _save(self) -> None:
        try:
            STATE_PATH.write_text(json.dumps(self._st, indent=1))
        except Exception:  # noqa: BLE001
            pass

    @property
    def consecutive_losses(self) -> int:
        return int(self._st.get("consecutive_losses", 0))

    # ── time helpers ────────────────────────────────────────────────────
    @staticmethod
    def session_date(now: datetime) -> date:
        """CME/Topstep session date — rolls at 18:00 ET (a trade at/after 18:00
        belongs to the next calendar day's session)."""
        return now.date() + timedelta(days=1) if now.hour >= 18 else now.date()

    # ── decisions (pure, no side effects) ───────────────────────────────
    def halted(self) -> bool:
        return self.consecutive_losses >= self.max_losing_nights

    def should_enter(self, now: datetime, is_flat: bool) -> tuple[bool, str]:
        """Enter once per session inside the entry window (weekdays; Sun eve opens Mon)."""
        mins = now.hour * 60 + now.minute
        if now.weekday() == 5:                      # Saturday: no session
            return False, "saturday"
        if not (self.entry_min <= mins < self.entry_min + self.entry_window_min):
            return False, "outside entry window"
        if not is_flat:
            return False, "not flat"
        if str(self.session_date(now)) == self._st.get("last_entry_session", ""):
            return False, "already entered this session"
        if self.halted():
            return False, f"loss-streak halt ({self.consecutive_losses} losing nights)"
        return True, "enter"

    def should_exit(self, now: datetime, have_position: bool) -> bool:
        """Exit the overnight position at/after 06:00 ET, up to just before the next
        18:00 entry — so a restarted/late bot still flattens it."""
        if not have_position:
            return False
        mins = now.hour * 60 + now.minute
        return self.exit_min <= mins < self.entry_min

    # ── state transitions (persist) ─────────────────────────────────────
    def mark_entered(self, now: datetime) -> None:
        self._st["last_entry_session"] = str(self.session_date(now))
        self._save()

    def record_result(self, pnl_usd: float, now: datetime | None = None) -> None:
        """Update the loss streak from a CLOSED overnight trade's realized P&L."""
        self._st["consecutive_losses"] = (self.consecutive_losses + 1) if pnl_usd < 0 else 0
        hist = self._st.setdefault("history", [])
        hist.append({"closed": (now or datetime.now(ET)).isoformat(timespec="minutes"),
                     "pnl": round(float(pnl_usd), 2),
                     "streak_after": self._st["consecutive_losses"]})
        self._st["history"] = hist[-200:]
        self._save()

    def stop_points(self, point_value: float) -> float:
        """$ stop -> point distance (for the entry Signal's ATR field)."""
        return self.stop_usd / point_value if point_value > 0 else 0.0
