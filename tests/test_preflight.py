"""Unit tests for the daily readiness preflight (preflight.py).

Fully offline: exercises the check functions against a fresh Report and asserts
the status classification + overall verdict / exit-code logic. No network, no
real broker (ProjectX stays in mock mode when creds are absent).
"""
from __future__ import annotations

import preflight
from preflight import (
    FAIL,
    INFO,
    PASS,
    WARN,
    Report,
    check_config,
    check_dependencies,
    check_kill_switch,
    check_state,
    check_topstep_risk,
    run_preflight,
)


def _status_of(rep: Report, title_substr: str) -> str:
    for c in rep.checks:
        if title_substr.lower() in c.title.lower():
            return c.status
    raise AssertionError(f"no check titled ~{title_substr!r} in {[c.title for c in rep.checks]}")


# ── Report verdict logic ─────────────────────────────────────────────────────
def test_report_failed_and_warned_flags():
    rep = Report()
    rep.add(PASS, "a")
    assert not rep.failed and not rep.warned
    rep.add(WARN, "b")
    assert rep.warned and not rep.failed
    rep.add(FAIL, "c")
    assert rep.failed


def test_info_status_is_not_a_blocker():
    rep = Report()
    rep.add(INFO, "info-only")
    assert not rep.failed and not rep.warned


# ── check_config ─────────────────────────────────────────────────────────────
def test_check_config_fails_when_validate_returns_errors(monkeypatch):
    # CONFIG is a frozen dataclass — patch the method on the class, not the instance.
    monkeypatch.setattr(type(preflight.CONFIG), "validate", lambda self: ["boom"])
    rep = Report()
    check_config(rep)
    assert _status_of(rep, "Config") == FAIL
    assert "boom" in rep.checks[0].detail


def test_check_config_passes_on_clean_validate(monkeypatch):
    monkeypatch.setattr(type(preflight.CONFIG), "validate", lambda self: [])
    rep = Report()
    check_config(rep)
    assert _status_of(rep, "Config") == PASS


# ── check_dependencies ───────────────────────────────────────────────────────
def test_check_dependencies_reports_core_present():
    rep = Report()
    check_dependencies(rep)
    # httpx / numpy / dotenv are hard requirements and installed in the test env.
    assert _status_of(rep, "Core dependencies") == PASS


# ── check_kill_switch ────────────────────────────────────────────────────────
def test_kill_switch_armed_is_fail(monkeypatch):
    import risk
    monkeypatch.setattr(risk, "kill_switch_active", lambda: True)
    rep = Report()
    check_kill_switch(rep)
    assert _status_of(rep, "Kill switch") == FAIL


def test_kill_switch_clear_is_pass(monkeypatch):
    import risk
    monkeypatch.setattr(risk, "kill_switch_active", lambda: False)
    rep = Report()
    check_kill_switch(rep)
    assert _status_of(rep, "Kill switch") == PASS


# ── check_topstep_risk ───────────────────────────────────────────────────────
def test_topstep_headroom_healthy_is_pass():
    rep = Report()
    rep.equity = 50_000.0
    check_topstep_risk(rep)
    c = next(c for c in rep.checks if "headroom" in c.title.lower())
    # $50k start → floor $48k → $2k headroom > $1k DLL → healthy.
    assert c.status == PASS


def test_topstep_headroom_breached_is_fail():
    rep = Report()
    rep.equity = 47_000.0  # below the $48k trailing-MLL floor
    check_topstep_risk(rep)
    c = next(c for c in rep.checks if "headroom" in c.title.lower())
    assert c.status == FAIL


def test_topstep_headroom_thin_is_warn():
    rep = Report()
    rep.equity = 48_500.0  # $500 headroom < $1k DLL → thin
    check_topstep_risk(rep)
    c = next(c for c in rep.checks if "headroom" in c.title.lower())
    assert c.status == WARN


# ── check_state ──────────────────────────────────────────────────────────────
def test_check_state_never_raises():
    rep = Report()
    check_state(rep)  # stateless (no DATABASE_URL) must not blow up
    assert any("state" in c.title.lower() for c in rep.checks)


# ── end-to-end ───────────────────────────────────────────────────────────────
def test_run_preflight_produces_all_checks_and_clean_stdout(capsys):
    rep = run_preflight()
    titles = {c.title for c in rep.checks}
    for expected in ("Config validation", "Core dependencies", "Kill switch",
                     "Broker connectivity", "Topstep risk headroom",
                     "Session timing", "Persisted state"):
        assert expected in titles, f"missing check: {expected}"
    # run_preflight routes advisory prints to stderr — stdout stays empty.
    assert capsys.readouterr().out == ""
