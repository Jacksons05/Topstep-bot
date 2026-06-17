"""Entrypoint. Continuous agentic loop with adaptive cadence + clean shutdown.

  python run.py            # loop, interval adapts to volatility
  python run.py --once     # single cycle (cron / testing)
"""
from __future__ import annotations

import sys
import time

from config import CONFIG
from dashboard import start_background as _start_dashboard
from engine import Engine
from notifier import notify


def main() -> int:
    errs = CONFIG.validate()
    if errs:
        for e in errs:
            print(f"CONFIG ERROR: {e}")
        return 1

    _start_dashboard()

    once = "--once" in sys.argv
    mode = "LIVE (real money)" if CONFIG.is_live else "paper"
    notify(f"=== JARVIS engine starting | mode={mode} | broker={CONFIG.broker} | "
           f"watchlist={len(CONFIG.watchlist)} | "
           f"llm={'on' if CONFIG.llm_ready else 'off'} | "
           f"options={CONFIG.options_source} ===")
    if CONFIG.is_live:
        notify("!!! LIVE MODE: real orders will be placed. Ctrl-C now to abort. !!!")
        time.sleep(5)

    engine = Engine()
    try:
        while True:
            try:
                engine.run_once()
            except Exception as e:  # noqa: BLE001 - never let one cycle kill the loop
                notify(f"cycle error: {e}")
            if once:
                break
            time.sleep(engine.next_interval())
    except KeyboardInterrupt:
        notify("shutdown requested")
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
