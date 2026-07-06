"""Overnight-drift strategy runner (validated: oos/HYPOTHESES.md Round 3).

Buys OD_QTY of OD_SYMBOL near the 16:00 ET close, exits at the next session's
09:30 ET open. No stop by design — the overnight gap risk IS the harvested
premium (stops cannot protect against gaps anyway). Validated net of costs on
two independent samples: MNQ 2019-2026 (PF 1.16, p=0.017) and NQ 2010-2019
(PF 1.14, p=0.022).

Runs standalone (launchd: com.topstep.overnight). Independent of engine.py.
Safety: honors the KILL_SWITCH file, OVERNIGHT_DRIFT_ENABLED env, and a state
file that prevents double entry. OD_DRY defaults to 1 (log, don't trade).

⛔ TOPSTEP GUARD: holding 16:00→09:30 violates Topstep's 16:10–18:00 flatten
rule, so run() UNCONDITIONALLY refuses the ProjectX/TopstepX gateway — no env
override exists. This runner only arms once a non-prop broker adapter (a
different broker class) is wired in; until then it is dormant scaffolding for
the one validated edge (kept per CLAUDE.md priority #1).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from risk import kill_switch_active  # noqa: E402

ET = ZoneInfo("America/New_York")
STATE_PATH = ROOT / "od_state.json"

ENABLED = os.getenv("OVERNIGHT_DRIFT_ENABLED", "0") == "1"
SYMBOL = os.getenv("OD_SYMBOL", "MNQ")
QTY = int(os.getenv("OD_QTY", "1"))
DRY = os.getenv("OD_DRY", "1") == "1"   # safe default: log, don't trade (set OD_DRY=0 to arm)
# Account-wide contract ceiling for the entry gate (matches the Topstep-style
# cap by default; adjust per venue when a non-prop adapter exists).
MAX_ACCOUNT_CONTRACTS = int(os.getenv("OD_MAX_ACCOUNT_CONTRACTS", "5"))

ENTRY_START = 15 * 60 + 55   # 15:55 ET
ENTRY_END = 16 * 60 + 10     # missed-window guard
EXIT_START = 9 * 60 + 30     # 09:30 ET
EXIT_END = 9 * 60 + 50


def log(msg: str) -> None:
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"[{now}] [overnight-drift] {msg}", flush=True)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1))


def last_price(broker, symbol: str) -> float | None:
    bars = broker.historical_bars(symbol, timeframe="5Min", limit=2, days_back=1)
    closes = bars.get("close") or []
    return float(closes[-1]) if closes else None


def broker_refused(broker) -> str | None:
    """Hard venue guard. This strategy HOLDS through 16:10–18:00 ET, which
    violates Topstep's daily flatten rule — running it against the
    ProjectX/TopstepX gateway is a rule breach regardless of any env flag,
    so there is deliberately NO override. It becomes runnable only when a
    non-prop broker adapter (different class) is wired in."""
    if broker.__class__.__name__ == "ProjectXBroker":
        return ("broker is the ProjectX/TopstepX gateway — overnight holds "
                "violate Topstep's 16:10–18:00 flatten rule (HYPOTHESES.md "
                "Round 3: legal only on a non-prop account)")
    return None


def entry_blocked(broker) -> str | None:
    """Pre-entry account gates (venue-agnostic). Fail CLOSED: if the position
    read errors, skip tonight's entry rather than stack unknown risk."""
    get_pos = getattr(broker, "get_positions", None)
    if not callable(get_pos):
        return None
    try:
        total = 0
        for p in get_pos():
            q = p.get("qty", 0) if isinstance(p, dict) else getattr(p, "qty", 0)
            total += abs(int(float(q or 0)))
        if total + QTY > MAX_ACCOUNT_CONTRACTS:
            return (f"account-wide contracts {total}+{QTY} would exceed the "
                    f"{MAX_ACCOUNT_CONTRACTS} cap")
    except Exception as exc:  # noqa: BLE001
        return f"position read failed ({exc}) — fail closed, no entry"
    return None


def run() -> None:
    if not ENABLED:
        log("OVERNIGHT_DRIFT_ENABLED != 1 — exiting")
        return
    from projectx_executor import ProjectXBroker
    broker = ProjectXBroker()
    refused = broker_refused(broker)
    if refused:
        log(f"REFUSING to run: {refused}")
        return
    if getattr(broker, "_mock_mode", True):
        log("broker in mock mode — no auth; exiting")
        return
    log(f"started | symbol={SYMBOL} qty={QTY} dry={DRY}")

    while True:
        try:
            now = datetime.now(ET)
            m = now.hour * 60 + now.minute
            state = load_state()
            holding = state.get("holding", False)
            today = str(date.today())

            if kill_switch_active():
                if holding and EXIT_START <= m < EXIT_END:
                    pass  # still allow the exit leg below even when killed
                else:
                    time.sleep(30)
                    continue

            # entry leg: weekday, entry window, flat, not already entered today
            if (not holding and now.weekday() < 5
                    and ENTRY_START <= m < ENTRY_END
                    and state.get("entry_date") != today):
                blocked = entry_blocked(broker)
                px = None if blocked else last_price(broker, SYMBOL)
                if blocked:
                    log(f"entry blocked: {blocked}")
                elif px is None:
                    log("no price — skip entry this cycle")
                elif DRY:
                    log(f"DRY entry: BUY {QTY} {SYMBOL} @ ~{px}")
                    save_state({"holding": True, "entry_date": today, "dry": True,
                                "entry_px": px})
                else:
                    fill = broker.submit(SYMBOL, float(QTY), "BUY", px)
                    log(f"ENTRY BUY {QTY} {SYMBOL} @ ~{px} order={getattr(fill, 'order_id', '?')}")
                    save_state({"holding": True, "entry_date": today, "dry": False,
                                "entry_px": px,
                                "order_id": str(getattr(fill, "order_id", ""))})

            # exit leg: holding and exit window on a later day
            elif (holding and EXIT_START <= m < EXIT_END
                  and state.get("entry_date") != today):
                px = last_price(broker, SYMBOL)
                if state.get("dry"):
                    log(f"DRY exit: SELL {QTY} {SYMBOL} @ ~{px}")
                    save_state({"holding": False, "entry_date": state.get("entry_date")})
                else:
                    fill = broker.submit(SYMBOL, float(QTY), "SELL", px or 0.0)
                    log(f"EXIT SELL {QTY} {SYMBOL} @ ~{px} order={getattr(fill, 'order_id', '?')}")
                    save_state({"holding": False, "entry_date": state.get("entry_date")})
        except Exception as exc:  # noqa: BLE001
            log(f"cycle error: {exc}")
        time.sleep(30)


if __name__ == "__main__":
    run()
