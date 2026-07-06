"""Daily readiness preflight — "is the bot ready to trade today?"

Run this each morning before arming the engine. It is READ-ONLY: it never
places, cancels, or modifies an order. It fans out a set of independent checks
(config, dependencies, kill switch, broker/ProjectX connectivity, Topstep risk
headroom, session timing, persisted state) and prints a single PASS/WARN/FAIL
report with an overall verdict.

    python preflight.py            # human-readable report
    python preflight.py --json     # machine-readable (cron / dashboards)

Exit codes:
    0  READY            — no blockers (may include non-fatal WARNs)
    2  NOT READY        — at least one FAIL; do not arm the engine until cleared

A FAIL is a hard blocker (invalid config, missing core dependency, kill switch
armed, ProjectX credentials present but the login is broken). A WARN is a
degraded-but-tradeable condition you should be aware of (running on the sim
fallback with no live creds, LLM off so the agent team is neutral, news feed
keyless, EOD flatten window already open, an econ-release blackout in effect).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from config import CONFIG

_ET = ZoneInfo("America/New_York")

PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
_MARK = {PASS: "✓", WARN: "⚠", FAIL: "✗", INFO: "•"}


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    # equity discovered during the broker check, threaded into the risk check.
    equity: float | None = None

    def add(self, status: str, title: str, detail: str = "") -> None:
        self.checks.append(Check(status, title, detail))

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    @property
    def warned(self) -> bool:
        return any(c.status == WARN for c in self.checks)


# ── individual checks ─────────────────────────────────────────────────────

def check_config(rep: Report) -> None:
    errs = CONFIG.validate()
    mode = "LIVE (real money)" if CONFIG.is_live else "paper"
    summary = (
        f"mode={mode} | broker={CONFIG.broker} | "
        f"topstep_mode={'on' if CONFIG.topstep_mode_enabled else 'off'} | "
        f"watchlist={','.join(CONFIG.watchlist)} | "
        f"llm={'on' if CONFIG.llm_ready else 'off'} | "
        f"news={'ready' if CONFIG.news_ready else 'off/keyless'}"
    )
    if errs:
        rep.add(FAIL, "Config validation", summary + "\n    " + "\n    ".join(errs))
    else:
        rep.add(PASS, "Config validation", summary)


def check_dependencies(rep: Report) -> None:
    import importlib.util

    def present(mod: str) -> bool:
        return importlib.util.find_spec(mod) is not None

    # Core runtime deps — the engine cannot run without these.
    missing_core = [m for m in ("httpx", "numpy", "dotenv") if not present(m)]
    if missing_core:
        rep.add(FAIL, "Core dependencies",
                f"missing: {', '.join(missing_core)} — pip install -r requirements.txt")
    else:
        rep.add(PASS, "Core dependencies", "httpx, numpy, python-dotenv present")

    # State persistence — optional but recommended for a 24/7 deploy.
    if present("psycopg2"):
        rep.add(PASS, "State backend", "psycopg2 present (Postgres-backed state available)")
    else:
        rep.add(WARN, "State backend",
                "psycopg2 absent — state is in-memory only (lost on restart)")

    # LLM client — only needed when the agent team is on.
    if CONFIG.llm_enabled and CONFIG.llm_backend == "anthropic":
        if present("anthropic"):
            rep.add(PASS, "LLM client", "anthropic SDK present")
        else:
            rep.add(FAIL, "LLM client",
                    "LLM_ENABLED but the anthropic SDK is missing — pip install anthropic")

    # Live order-flow feed — only needed when running live futures via ProjectX.
    if CONFIG.topstep_mode_enabled and CONFIG.projectx_username and CONFIG.projectx_api_key:
        if present("signalrcore"):
            rep.add(PASS, "Order-flow feed", "signalrcore present (ProjectX SignalR hub)")
        else:
            rep.add(WARN, "Order-flow feed",
                    "signalrcore absent — the live OBI/CVD/L2 gate degrades to a no-op "
                    "(fails open); pip install signalrcore for the confirmation gate")

    # Optional accelerators — informational only.
    opt = [m for m in ("lightgbm", "polars", "numba", "pyarrow") if present(m)]
    rep.add(INFO, "Optional accelerators",
            (", ".join(opt) + " present") if opt else
            "none of lightgbm/polars/numba/pyarrow (fallbacks in use)")


def check_kill_switch(rep: Report) -> None:
    try:
        from risk import kill_switch_active
    except Exception as e:  # noqa: BLE001
        rep.add(WARN, "Kill switch", f"could not evaluate: {e}")
        return
    if kill_switch_active():
        rep.add(FAIL, "Kill switch",
                f"ARMED ({CONFIG.kill_switch_file} present or KILL_SWITCH=1) — "
                "new entries are halted; remove it to trade")
    else:
        rep.add(PASS, "Kill switch", "clear")


def check_broker(rep: Report) -> None:
    """Broker / ProjectX connectivity. Read-only: fetches the account balance
    only. Sets rep.equity when a live balance is read (feeds the risk check)."""
    if not (CONFIG.topstep_mode_enabled and CONFIG.projectx_username and CONFIG.projectx_api_key):
        rep.add(WARN, "Broker connectivity",
                "ProjectX credentials not set — running on the sim/paper fallback. "
                "Set PROJECTX_USERNAME + PROJECTX_API_KEY to trade futures live.")
        return

    broker = None
    try:
        from projectx_executor import ProjectXBroker
        broker = ProjectXBroker()
        if getattr(broker, "_mock_mode", True):
            rep.add(FAIL, "Broker connectivity",
                    "ProjectX credentials are set but login FAILED — broker is in MOCK "
                    "mode. A 'live' run here would place no real orders. Check "
                    "PROJECTX_USERNAME / PROJECTX_API_KEY and network.")
            return
        acct = broker.account()
        equity = float(acct.get("equity", 0.0) or 0.0)
        rep.equity = equity
        env = "live/funded" if CONFIG.projectx_live else "sim/eval"
        rep.add(PASS, "Broker connectivity",
                f"ProjectX connected | account={acct.get('account_id', '?')} | "
                f"equity=${equity:,.2f} | env={env}")
    except Exception as e:  # noqa: BLE001
        rep.add(FAIL, "Broker connectivity", f"ProjectX connect/account failed: {e}")
    finally:
        if broker is not None:
            try:
                broker.close()
            except Exception:  # noqa: BLE001
                pass


def check_topstep_risk(rep: Report) -> None:
    try:
        from topstep_risk import TopstepRiskManager
    except Exception as e:  # noqa: BLE001
        rep.add(WARN, "Topstep risk headroom", f"could not evaluate: {e}")
        return

    equity = rep.equity if rep.equity is not None else CONFIG.topstep_account_size
    src = "live balance" if rep.equity is not None else "configured account size (no live read)"
    ts = TopstepRiskManager(initial_equity=equity)
    floor = ts.mll_floor()
    headroom = equity - floor
    dll = CONFIG.topstep_daily_loss_limit
    detail = (
        f"equity=${equity:,.2f} ({src}) | trailing-MLL floor=${floor:,.2f} | "
        f"headroom=${headroom:,.2f} | daily-loss-limit=${dll:,.0f} | "
        f"max-contracts={CONFIG.topstep_max_contracts} (account-wide)"
    )
    if headroom <= 0:
        rep.add(FAIL, "Topstep risk headroom",
                "equity is AT/BELOW the trailing Max Loss Limit floor — account would "
                "liquidate on the next tick. " + detail)
    elif headroom < dll:
        rep.add(WARN, "Topstep risk headroom",
                "headroom to the MLL floor is below one Daily Loss Limit — thin. " + detail)
    else:
        rep.add(PASS, "Topstep risk headroom", detail)


def check_session(rep: Report) -> None:
    now = datetime.now(_ET)
    line = f"now={now:%Y-%m-%d %H:%M:%S} ET | weekday={now:%A}"

    if now.weekday() >= 5:  # Sat/Sun
        rep.add(WARN, "Session timing",
                "weekend — the futures market is closed for most of the session. " + line)
        return

    try:
        from topstep_risk import TopstepRiskManager
        ts = TopstepRiskManager()
        flat = ts.should_flatten_now()
        near, why = ts.near_economic_release(CONFIG.topstep_econ_blackout_min)
    except Exception as e:  # noqa: BLE001
        rep.add(WARN, "Session timing", f"could not evaluate windows: {e} | {line}")
        return

    notes = []
    status = PASS
    if flat:
        status = WARN
        notes.append(f"EOD flatten window OPEN (≥ {CONFIG.topstep_flatten_time} ET) — "
                     "no new entries will be taken")
    if near:
        status = WARN
        notes.append(f"econ-release blackout active: {why}")
    detail = line + (" | " + " | ".join(notes) if notes else " | flatten window closed, no econ blackout")
    rep.add(status, "Session timing", detail)


def check_state(rep: Report) -> None:
    try:
        from state import State
        st = State.load()
        n_open = len(st.open_positions)  # property; already excludes shadow book
        day_pnl = st.daily_pnl()
        rep.add(INFO, "Persisted state",
                f"open positions={n_open} | day P&L=${day_pnl:,.2f} | "
                f"realized=${st.realized_pnl_usd:,.2f}")
    except Exception as e:  # noqa: BLE001
        rep.add(WARN, "Persisted state", f"could not load state: {e}")


CHECKS = [
    check_config,
    check_dependencies,
    check_kill_switch,
    check_broker,
    check_topstep_risk,
    check_session,
    check_state,
]


def run_preflight() -> Report:
    # Some checks call into modules (CONFIG.validate, State.load) that print
    # advisory WARNINGs straight to stdout. Route those to stderr while the
    # checks run so stdout carries only the report / JSON payload — the same
    # information is surfaced as structured checks anyway.
    rep = Report()
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        for chk in CHECKS:
            try:
                chk(rep)
            except Exception as e:  # noqa: BLE001 - a check must never crash the preflight
                rep.add(WARN, chk.__name__, f"check raised: {e}")
    finally:
        sys.stdout = real_stdout
    return rep


def _render_human(rep: Report) -> str:
    lines = ["", "═══ JARVIS Topstep — daily readiness preflight ═══", ""]
    for c in rep.checks:
        lines.append(f"  [{_MARK[c.status]}] {c.status:4} {c.title}")
        if c.detail:
            for dl in c.detail.split("\n"):
                lines.append(f"        {dl}")
    lines.append("")
    if rep.failed:
        verdict = "✗ NOT READY — resolve the FAIL(s) above before arming the engine."
    elif rep.warned:
        verdict = "✓ READY (with warnings) — tradeable, but review the WARN(s) above."
    else:
        verdict = "✓ READY — all checks green."
    lines.append(verdict)
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    rep = run_preflight()
    if "--json" in sys.argv:
        payload = {
            "ready": not rep.failed,
            "verdict": "not_ready" if rep.failed else ("ready_with_warnings" if rep.warned else "ready"),
            "checks": [{"status": c.status, "title": c.title, "detail": c.detail} for c in rep.checks],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(_render_human(rep))
    return 2 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
