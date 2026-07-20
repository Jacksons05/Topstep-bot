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
from preflight import run_preflight


def _print_preflight(rep) -> None:
    print("")
    print("=== preflight ===")
    for c in rep.checks:
        line = f"  [{c.status:4}] {c.title}"
        print(line)
        if c.detail:
            for dl in c.detail.split("\n"):
                print(f"         {dl}")
    print("")


def main() -> int:
    errs = CONFIG.validate()
    if errs:
        for e in errs:
            print(f"CONFIG ERROR: {e}")
        return 1

    # Daily readiness preflight (config, kill switch, broker/ProjectX login,
    # Topstep MLL headroom, session timing, persisted state) — a FAIL here is
    # a hard blocker (see preflight.py). Previously this check existed but was
    # never invoked by the entrypoint, so a creds-present-but-auth-failing
    # ProjectX login could silently drop to mock while the engine believed it
    # was live. Run it before anything is armed, and refuse to start on FAIL.
    preflight_rep = run_preflight()
    _print_preflight(preflight_rep)
    if preflight_rep.failed:
        print("PREFLIGHT FAIL — refusing to start. Resolve the FAIL(s) above "
              "(`python preflight.py` for the full report) and restart.")
        return 2

    _start_dashboard()

    once = "--once" in sys.argv
    engine = Engine()
    real_money = CONFIG.is_live or engine._live_projectx()
    mode = "LIVE (real money)" if real_money else "paper"
    notify(f"=== JARVIS engine starting | mode={mode} | broker={CONFIG.broker} | "
           f"watchlist={len(CONFIG.watchlist)} | "
           f"llm={'on' if CONFIG.llm_ready else 'off'} | "
           f"options={CONFIG.options_source} ===")
    if real_money:
        notify("!!! LIVE MODE: real orders will be placed. Ctrl-C now to abort. !!!")
        time.sleep(5)

    try:
        while True:
            try:
                engine.run_once()
            except Exception as e:  # noqa: BLE001 - never let one cycle kill the loop
                notify(f"cycle error: {e}")
            if once:
                break
            # Event-driven wake: fires immediately on a live order-flow tick
            # (quote/trade/depth) crossing a fresh bar boundary, falling back
            # to a plain timeout (identical to the old time.sleep) when no
            # feed is attached or EVENT_DRIVEN_LOOP_ENABLED=false.
            engine.wake_wait(engine.next_interval())
    except KeyboardInterrupt:
        notify("shutdown requested")
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
