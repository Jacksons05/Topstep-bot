"""Live MES MBO forward-capture (Databento Live API, GLBX.MDP3).

Exists to build the forward/live confirmation window Round 20's own verdict
rule requires (see oos/HYPOTHESES.md "Round 20"), and to accumulate future
MBO-dependent research data without repeated paid historical pulls.

Explicitly NOT wired into the live trading engine's own quant_signal() /
entry decisions -- those run on ProjectX's feed and the SMA/RSI signal
already confirmed dead (Rounds 1 and 19). This is a pure research capture,
same spirit as uw_intraday_capture.py in the sister repo.

Runs continuously, one DBN file per calendar day (ET). Uses the Live
client's own RECONNECT policy for transient drops; exits cleanly (code 0) at
day rollover or an unrecoverable disconnect so a process supervisor
(systemd/Task Scheduler with restart-on-exit) starts fresh for the next day.

Usage: .venv/bin/python live_capture_mbo.py
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

import databento as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("live_mbo_capture")

DATASET = "GLBX.MDP3"
SCHEMA = "mbo"
SYMBOL = "MES.v.0"
OUT_DIR = ROOT / "oos" / "data" / "live_mbo"
ET = ZoneInfo("America/New_York")
ROLLOVER_CHECK_SEC = 30.0


def _today_path(d) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / f"MES_mbo_live_{d.isoformat()}.dbn.zst"


def _day_rollover_watcher(client: "db.Live", start_date, stop_event: threading.Event) -> None:
    """Calls client.stop() (a clean close) the first time the ET calendar
    date changes, so main() can exit 0 and let the supervisor start a fresh
    process/file for the new day. Also exits promptly if stop_event is set
    by main() for any other reason (avoids leaking this thread)."""
    while not stop_event.wait(ROLLOVER_CHECK_SEC):
        if datetime.now(ET).date() != start_date:
            log.info("day rollover detected -- stopping cleanly for supervisor restart")
            client.stop()
            return


def main() -> int:
    key = os.environ.get("DATABENTO_API_KEY", "")
    if not key:
        log.error("DATABENTO_API_KEY missing from .env")
        return 1

    start_date = datetime.now(ET).date()
    out_path = _today_path(start_date)
    log.info(f"starting live MBO capture: {SYMBOL} ({DATASET}/{SCHEMA}) -> {out_path}")

    # NONE, not RECONNECT: a persistent failure (e.g. an entitlement/auth
    # error) is not transient, and the client's own reconnect loop cannot
    # tell the difference -- it will hammer the gateway indefinitely rather
    # than surface the error. A clean exit + external supervisor restart
    # (systemd RestartSec, Task Scheduler retry delay) rate-limits retries
    # sanely either way, transient or not.
    client = db.Live(key, reconnect_policy=db.ReconnectPolicy.NONE)
    client.add_stream(str(out_path))
    client.subscribe(
        dataset=DATASET, schema=SCHEMA, symbols=[SYMBOL], stype_in="continuous",
    )
    client.start()

    stop_event = threading.Event()
    watcher = threading.Thread(
        target=_day_rollover_watcher, args=(client, start_date, stop_event), daemon=True,
    )
    watcher.start()

    try:
        client.block_for_close()  # no timeout: blocks until stop()/terminate() or an
                                   # unrecoverable disconnect (reconnect_policy already
                                   # handles transient drops under the hood)
    except db.BentoError as e:
        log.warning(f"live session ended with an error: {e} -- exiting for supervisor restart")
        stop_event.set()
        return 1
    except Exception as e:  # noqa: BLE001 -- never let an unexpected exception hang the watcher thread
        log.error(f"unexpected error: {e} -- exiting for supervisor restart")
        stop_event.set()
        return 1

    stop_event.set()
    log.info("session closed cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
