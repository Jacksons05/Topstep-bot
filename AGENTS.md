# AGENTS.md

Trading rules, methodology, and account constraints live in `CLAUDE.md` and
`.claude/review-role.md` — read those before touching any strategy/trading
logic. This file only covers how to develop/run the codebase in the Cursor
Cloud environment.

## Cursor Cloud specific instructions

This is a **Python 3.12** project (see `.python-version`). Dependencies are
installed into a virtualenv at `.venv/` by the startup update script; activate
it with `source .venv/bin/activate` before running anything. (Creating the
venv requires the `python3.12-venv` system package.)

### Offline / no-keys dev mode
The bot runs fully offline with no API keys in **sim** mode — this is the
documented dry-run path (README "Setup"). The repo is `.gitignore`-d for
`.env`; for local dev create one with at least:

```
BROKER=sim
TRADING_MODE=paper
LLM_ENABLED=false
TOPSTEP_MODE_ENABLED=false
```

Without those, `CONFIG.validate()` fails fast (default `.env.example` assumes
`BROKER=alpaca` + `LLM_ENABLED=true`, which require keys).

**Expected offline behavior (not a bug):** with no market-data credentials the
Alpaca feed returns empty, so the regime/intraday-change lookup is unavailable
→ the circuit breaker **fails closed (RED)** → no new entries. The full
agentic cycle still runs, scans, and reports every loop. To exercise a real
(paper) trade end-to-end you need market-data + broker keys (`ALPACA_API_KEY`
/ `ALPACA_SECRET_KEY`, paper) and, for the agent team, `ANTHROPIC_API_KEY`.

### Run / test / lint
- Tests: `python -m pytest tests/` — deterministic, no network, no keys.
- Lint: there is **no configured linter** (no ruff/flake8/pre-commit/CI); the
  test suite is the only automated gate.
- Readiness check: `python preflight.py` (read-only; never places an order).
- Single cycle: `python run.py --once`. Continuous loop: `python run.py`.
- Dashboard (JARVIS command center) starts with `run.py` at
  `http://127.0.0.1:8787`. Bind it on all interfaces with `DASH_BIND=0.0.0.0`
  (or run standalone: `python dashboard.py`). It polls `/api/status` every 4s.

### State
`DATABASE_URL` (Postgres) is optional locally — without it the bot runs
stateless and positions do not survive a restart (it logs a warning).
