"""Process-level singleton lock for the trading engine.

Nothing in engine.py or state.py prevents two `run.py` processes from running
concurrently against the same ProjectX account: the Postgres advisory lock in
state.py serializes *writes to the state table*, but it does nothing to stop
two independent processes from each independently deciding "no position is
open, submit an order" and both submitting — a duplicate-order path that
exists purely because nothing enforces "only one engine process at a time."

This is a plain PID-file lock (no external dependency, no DB round trip):
  * Acquired once at Engine startup, released on clean shutdown.
  * Uses an atomic O_CREAT|O_EXCL open so two processes racing to acquire it
    at the same instant can't both succeed (no TOCTOU window).
  * A lock file left behind by a process that crashed without releasing it is
    detected as stale (the PID inside is no longer running) and reclaimed
    automatically — a hard crash must never permanently block every future
    restart.
"""
from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_LOCK_PATH = Path(__file__).resolve().parent / "ENGINE.lock"


class EngineAlreadyRunning(RuntimeError):
    """Another engine process holds the singleton lock and is still alive."""


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check. Fails safe: if we can't tell, assume alive
    (a false 'still running' blocks a restart with a clear error; a false
    'dead' would let a second live engine start trading — much worse)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def acquire(path: Path | str = _DEFAULT_LOCK_PATH) -> Path:
    """Claim the singleton lock or raise EngineAlreadyRunning.

    Returns the lock path (pass it to release() on shutdown). Reclaims a
    stale lock (owning PID no longer alive) automatically, then retries the
    atomic create exactly once — a legitimate second live process racing on
    the retry will simply fail the second O_EXCL create and raise.
    """
    path = Path(path)
    for _attempt in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                old_pid = int(path.read_text().strip())
            except (ValueError, OSError):
                old_pid = -1
            if old_pid == os.getpid():
                return path  # already ours somehow — treat as acquired
            if _pid_alive(old_pid):
                raise EngineAlreadyRunning(
                    f"another engine process (pid={old_pid}) is already running "
                    f"(lock: {path}) — refusing to start a second instance "
                    f"against the same account/state. If that process is "
                    f"confirmed gone, delete {path} and retry."
                )
            # Stale lock from a process that's no longer running — reclaim.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        else:
            with os.fdopen(fd, "w") as f:
                f.write(str(os.getpid()))
            return path
    raise EngineAlreadyRunning(
        f"could not acquire {path} — a competing process claimed it during "
        f"stale-lock reclamation; refusing to start"
    )


def release(path: Path | str = _DEFAULT_LOCK_PATH) -> None:
    """Release the lock iff this process still owns it. Safe to call multiple
    times / on a path that was never acquired (e.g. Engine init failed before
    acquire() ran) — never raises."""
    path = Path(path)
    try:
        if path.exists() and int(path.read_text().strip()) == os.getpid():
            path.unlink()
    except (ValueError, OSError):
        pass
