"""Global pytest configuration.

Makes the project root importable (so `import signals` works) and provides
session-wide fixtures that apply to every test automatically.

Autouse fixtures
----------------
silence_notifier
    Patches ``engine.notify`` (and, transitively, the same ``notify``
    function everywhere it is imported at module level — ``executor``,
    ``topstep_risk``, ``singleton_lock``, etc.) to a no-op for the
    entire test session.

    Without this, any test that exercises code paths that call ``notify()``
    (kill-switch halt, manage-open exits, Topstep mode init, …) emits
    timestamped lines to stdout and appends them to ``signals.log`` — both
    of which pollute the dashboard feed and make ``pytest -s`` output nearly
    unreadable.  Tests that need to *inspect* notifications can override
    this by injecting their own ``monkeypatch.setattr(eng, "notify", ...)``
    after the fixture has already run, or by using ``capfd`` to capture the
    output instead.

    The fixture patches every module that imports ``notify`` at the top
    level so the no-op is consistent regardless of which import path the
    code under test uses.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def silence_notifier(monkeypatch) -> None:
    """Suppress all notify() calls for the duration of every test.

    Patches the name in each module that imports it at the top level so
    every call site sees the same no-op, regardless of how the module was
    imported.  The log file is also suppressed (the fixture sets the name
    used by notifier.log, which notify() delegates to).

    Individual tests that need to assert on notifications should use
    monkeypatch.setattr(module, "notify", collector_fn) AFTER this fixture
    has run — monkeypatch stacks, and the last patch wins.
    """
    _noop = lambda *a, **k: None  # noqa: E731
    # Patch the notifier module's own output functions first: notify() calls
    # notifier.log() which calls print() and writes to signals.log.
    import importlib
    try:
        notifier_mod = importlib.import_module("notifier")
        # Silence the low-level output function that notify() delegates to.
        # notify() itself is re-exported into other modules; silencing it here
        # is sufficient when those modules import via `from notifier import notify`.
        monkeypatch.setattr(notifier_mod, "log", _noop)
        monkeypatch.setattr(notifier_mod, "notify", _noop)
    except ImportError:
        pass
    # Also patch the `notify` name in every module that does a top-level
    # `from notifier import notify` — those captured the function at import
    # time and won't see the patch on the notifier module itself.
    for mod_name in (
        "engine",
        "executor",
        "topstep_risk",
        "singleton_lock",
        "preflight",
        "trading_status",
        "day_learner",
    ):
        try:
            mod = importlib.import_module(mod_name)
            if hasattr(mod, "notify") and callable(mod.notify):
                monkeypatch.setattr(mod, "notify", _noop)
        except ImportError:
            pass
