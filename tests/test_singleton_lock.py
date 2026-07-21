"""Unit tests for singleton_lock.py — the process-level lock added to close
the "two engine processes on one account" duplicate-order gap.

Fully offline: uses tmp_path for the lock file, no network, no real PIDs
beyond the test process's own (always alive) and a deliberately-invalid one
(guaranteed not alive) for the stale-lock-reclaim path.
"""
from __future__ import annotations

import os
import sys

import pytest

import singleton_lock


def test_acquire_then_release_is_clean(tmp_path):
    lock = tmp_path / "ENGINE.lock"
    p = singleton_lock.acquire(lock)
    assert p == lock
    assert lock.exists()
    assert lock.read_text().strip() == str(os.getpid())
    singleton_lock.release(lock)
    assert not lock.exists()


def test_second_acquire_by_a_live_process_is_rejected(tmp_path, monkeypatch):
    lock = tmp_path / "ENGINE.lock"
    other_pid = os.getpid() + 12345  # some PID that is not this test process
    lock.write_text(str(other_pid))
    # Force the liveness check to say "yes, still running" for that PID
    # without needing to actually spawn a second process.
    monkeypatch.setattr(singleton_lock, "_pid_alive", lambda pid: pid == other_pid)
    with pytest.raises(singleton_lock.EngineAlreadyRunning):
        singleton_lock.acquire(lock)
    # The rejected acquire attempt must not have deleted or corrupted the
    # existing lock file.
    assert lock.read_text().strip() == str(other_pid)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.kill(pid, 0) is POSIX-shaped: on Linux/macOS a never-allocated "
           "PID reliably raises ProcessLookupError, which _pid_alive maps to "
           "'dead' and this test exercises the reclaim path. On Windows the "
           "same call raises a plain OSError (WinError 87) that _pid_alive "
           "can't distinguish from a real 'can't tell' case, so it correctly "
           "fails safe and assumes alive (see singleton_lock._pid_alive's "
           "docstring, fixed 2026-07-20) — this exact reclaim scenario isn't "
           "reachable on Windows via this technique. The actual production "
           "target is Linux, where this path is real and this test matters.")
def test_stale_lock_from_a_dead_pid_is_reclaimed(tmp_path):
    lock = tmp_path / "ENGINE.lock"
    # PID 1 followed by a large offset is astronomically unlikely to be a
    # real running process in any sandbox; a guaranteed-invalid PID (very
    # large, unlikely to ever be assigned) is what matters here — the
    # reclaim path is exercised as long as os.kill(pid, 0) raises
    # ProcessLookupError, which it will for a PID that was never allocated.
    dead_pid = 2**30 - 1
    lock.write_text(str(dead_pid))
    p = singleton_lock.acquire(lock)
    assert p == lock
    assert lock.read_text().strip() == str(os.getpid()), (
        "a stale lock (owning PID no longer running) must be reclaimed, not "
        "treated as 'another engine is running'"
    )
    singleton_lock.release(lock)


def test_release_of_a_lock_owned_by_another_pid_is_a_noop(tmp_path):
    lock = tmp_path / "ENGINE.lock"
    lock.write_text(str(os.getpid() + 1))  # pretend another process owns it
    singleton_lock.release(lock)
    assert lock.exists(), "release() must never delete a lock this process doesn't own"


def test_release_without_prior_acquire_never_raises(tmp_path):
    lock = tmp_path / "does-not-exist.lock"
    singleton_lock.release(lock)  # must be a silent no-op, not an exception


def test_corrupt_lock_file_content_does_not_crash_release(tmp_path):
    lock = tmp_path / "ENGINE.lock"
    lock.write_text("not-a-pid")
    singleton_lock.release(lock)   # must not raise ValueError
    assert lock.exists()           # unrecognized content → leave it alone, don't delete
