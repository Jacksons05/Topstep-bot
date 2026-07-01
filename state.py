"""Position + P&L tracking, persisted to Postgres via DATABASE_URL.

Railway injects DATABASE_URL when you add a Postgres service. Falls back to
stateless (in-memory) mode when it's absent — fine for local runs, just know
state is lost on restart.

Tracks two books in one table: the real book and the Cramer-mode inverse
shadow book (`shadow=TRUE`), so you can compare whether inverting the signals
would have done better.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

DATABASE_URL: str = os.getenv("DATABASE_URL", "")

_DDL_POSITIONS = """
CREATE TABLE IF NOT EXISTS positions (
    symbol      TEXT    NOT NULL,
    asset       TEXT    NOT NULL DEFAULT 'equity',
    opened_at   TEXT    NOT NULL,
    side        TEXT    NOT NULL DEFAULT 'BUY',
    qty         REAL    NOT NULL DEFAULT 0,
    entry_price REAL    NOT NULL DEFAULT 0,
    size_usd    REAL    NOT NULL DEFAULT 0,
    stop        REAL    NOT NULL DEFAULT 0,
    target      REAL    NOT NULL DEFAULT 0,
    kind        TEXT    NOT NULL DEFAULT '',
    thesis      TEXT    NOT NULL DEFAULT '',
    mode        TEXT    NOT NULL DEFAULT 'paper',
    shadow      BOOLEAN NOT NULL DEFAULT FALSE,
    open        BOOLEAN NOT NULL DEFAULT TRUE,
    exit_price  REAL,
    closed_at   TEXT,
    pnl_usd     REAL    NOT NULL DEFAULT 0,
    order_id    TEXT    NOT NULL DEFAULT '',
    filled      BOOLEAN NOT NULL DEFAULT TRUE,
    contract    TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (symbol, opened_at, shadow)
)
"""

# Additive migration for DBs created before order-fill reconciliation existed.
_DDL_MIGRATE = [
    "ALTER TABLE positions ADD COLUMN IF NOT EXISTS order_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE positions ADD COLUMN IF NOT EXISTS filled BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE positions ADD COLUMN IF NOT EXISTS contract TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE state_meta ADD COLUMN IF NOT EXISTS trading_days TEXT NOT NULL DEFAULT ''",
]

_DDL_META = """
CREATE TABLE IF NOT EXISTS state_meta (
    id            INTEGER PRIMARY KEY DEFAULT 1,
    realized_pnl  REAL    NOT NULL DEFAULT 0,
    shadow_pnl    REAL    NOT NULL DEFAULT 0,
    day           TEXT    NOT NULL DEFAULT '',
    day_start_pnl REAL    NOT NULL DEFAULT 0,
    trading_days  TEXT    NOT NULL DEFAULT '',
    CONSTRAINT single_row CHECK (id = 1)
)
"""


def _dsn() -> str:
    url = DATABASE_URL
    if not url or "sslmode=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}sslmode=require"


# Cap any single statement so a stuck lock (e.g. the positions-table deadlock)
# raises instead of hanging the whole cycle forever. Override with DB_STATEMENT_TIMEOUT_MS.
_STMT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "15000"))
_CONNECT_TIMEOUT_S = int(os.getenv("DB_CONNECT_TIMEOUT_S", "10"))

# ── Deadlock retry ────────────────────────────────────────────────────────────
# PostgreSQL resolves deadlocks by aborting one of the two transactions and
# raising DeadlockDetected.  Retrying after a short back-off is safe because
# the aborted transaction made no partial writes (the whole txn was rolled back).
_DEADLOCK_RETRIES = 3       # max attempts before propagating the error
_DEADLOCK_BACKOFF_S = 0.05  # 50 ms base; multiplied by attempt index (1×, 2×, …)

# ── Write serialization (advisory lock) ─────────────────────────────────────────
# All state-mutating transactions (schema setup, save) take this session-wide
# Postgres advisory lock as their FIRST statement.  pg_advisory_xact_lock blocks
# until the lock is free and auto-releases at transaction end, so only one writer
# is ever inside a positions/state_meta transaction at a time — across threads
# AND across separate processes (engine loop vs. the dashboard's State.load()).
# This is what actually eliminates the deadlock: an ALTER TABLE (ACCESS EXCLUSIVE)
# from a stray load() can no longer interleave with the engine's positions upsert.
_WRITE_LOCK_KEY = 0x7A1C_B07  # arbitrary constant shared by every writer

# Schema DDL (CREATE/ALTER) is idempotent but each ALTER grabs an ACCESS
# EXCLUSIVE lock, so we only run it once per process instead of on every load().
_schema_ready = False
_schema_lock = threading.Lock()

# ── Connection pool ───────────────────────────────────────────────────────────
# Reusing connections avoids a fresh SSL handshake on every save() call and
# keeps the connection count low on hosted Postgres (Railway, Supabase, etc.).
# ThreadedConnectionPool uses an internal lock so it is safe across threads.
_pool: "psycopg2.pool.ThreadedConnectionPool | None" = None
_pool_lock = threading.Lock()


def _get_pool():
    """Return the module-level ThreadedConnectionPool, created lazily on first use."""
    import psycopg2.pool
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=5,
                    dsn=_dsn(),
                    connect_timeout=_CONNECT_TIMEOUT_S,
                    options=f"-c statement_timeout={_STMT_TIMEOUT_MS}",
                )
    return _pool


@contextmanager
def _db():
    """Yield a pooled connection; commit on success, rollback on any exception."""
    conn = _get_pool().getconn()
    try:
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _get_pool().putconn(conn)


def _ensure_schema() -> None:
    # Run the DDL at most once per process. Repeated ALTER TABLE calls (one per
    # State.load(), including every dashboard poll) take ACCESS EXCLUSIVE locks
    # that deadlock against the engine's concurrent positions upsert.
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _db() as conn:
            with conn.cursor() as cur:
                # Serialize against any concurrent writer before touching locks.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK_KEY,))
                cur.execute(_DDL_POSITIONS)
                cur.execute(_DDL_META)
                for stmt in _DDL_MIGRATE:
                    cur.execute(stmt)
                cur.execute("INSERT INTO state_meta (id) VALUES (1) ON CONFLICT DO NOTHING")
        _schema_ready = True


@dataclass
class Position:
    symbol: str
    asset: str
    side: str
    qty: float
    entry_price: float
    size_usd: float
    stop: float
    target: float
    kind: str
    thesis: str
    opened_at: str
    mode: str
    shadow: bool = False
    open: bool = True
    exit_price: float | None = None
    closed_at: str | None = None
    pnl_usd: float = 0.0
    order_id: str = ""
    filled: bool = True       # False = entry recorded at ref price, awaiting real fill
    contract: str = ""        # option leg(s) as JSON for asset=="option" (empty for equity)
    # ProjectX orderId of the native exchange-resting protective stop (futures).
    # In-memory only (not persisted): after a restart the stop still rests on the
    # exchange, but the bot relies on position reconciliation to re-sync.
    protective_order_id: str = ""

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side == "BUY" else -self.qty


@dataclass
class State:
    positions: list[Position] = field(default_factory=list)
    realized_pnl_usd: float = 0.0
    shadow_pnl_usd: float = 0.0
    day: str = ""
    day_start_pnl: float = 0.0
    trading_days: set[str] = field(default_factory=set)

    @classmethod
    def load(cls) -> "State":
        if not DATABASE_URL:
            print("WARNING: DATABASE_URL not set — running stateless "
                  "(positions will not survive restarts)")
            return cls(day=_today())

        _ensure_schema()
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT realized_pnl, shadow_pnl, day, day_start_pnl, trading_days "
                            "FROM state_meta WHERE id = 1")
                row = cur.fetchone()
                realized = float(row[0]) if row else 0.0
                shadow = float(row[1]) if row else 0.0
                day = str(row[2]) if row else _today()
                day_start = float(row[3]) if row else 0.0
                trading_days = set(row[4].split(",")) if row and row[4] else set()

                cur.execute(
                    "SELECT symbol, asset, side, qty, entry_price, size_usd, stop, "
                    "target, kind, thesis, opened_at, mode, shadow, open, "
                    "exit_price, closed_at, pnl_usd, order_id, filled, contract FROM positions"
                )
                positions = [
                    Position(
                        symbol=r[0], asset=r[1], side=r[2], qty=float(r[3]),
                        entry_price=float(r[4]), size_usd=float(r[5]), stop=float(r[6]),
                        target=float(r[7]), kind=r[8], thesis=r[9], opened_at=r[10],
                        mode=r[11], shadow=bool(r[12]), open=bool(r[13]),
                        exit_price=float(r[14]) if r[14] is not None else None,
                        closed_at=r[15], pnl_usd=float(r[16]),
                        order_id=r[17] or "", filled=bool(r[18]), contract=r[19] or "",
                    )
                    for r in cur.fetchall()
                ]

        s = cls(positions=positions, realized_pnl_usd=realized, shadow_pnl_usd=shadow,
                day=day, day_start_pnl=day_start, trading_days=trading_days)
        s._roll_day()
        return s

    def save(self) -> None:
        """Persist state to Postgres with automatic deadlock retry.

        When PostgreSQL detects a deadlock it aborts one of the conflicting
        transactions and raises DeadlockDetected.  The aborted transaction
        made no partial writes (it was fully rolled back), so it is safe to
        retry the whole save from scratch.  We retry up to _DEADLOCK_RETRIES
        times with a linearly increasing back-off before giving up.
        """
        if not DATABASE_URL:
            return
        import psycopg2.errors
        for attempt in range(_DEADLOCK_RETRIES):
            try:
                self._save_once()
                return
            except psycopg2.errors.DeadlockDetected:
                if attempt < _DEADLOCK_RETRIES - 1:
                    time.sleep(_DEADLOCK_BACKOFF_S * (attempt + 1))
                else:
                    raise  # exhausted all retries — propagate so the caller can log it

    def _save_once(self) -> None:
        """Single transactional attempt to flush all in-memory state to Postgres.

        Deadlock-prevention strategy (two layers):

        1. Sort positions by primary key (symbol, opened_at, shadow) before
           writing so every concurrent writer acquires row-level locks in the
           same order.  This eliminates the classical cycle where transaction A
           locks AAPL then waits for TSLA while transaction B holds TSLA and
           waits for AAPL.

        2. Issue a SELECT … FOR UPDATE on all *existing* rows (in that same
           sorted order) at the top of the transaction, before any INSERT /
           UPDATE.  This converts the implicit per-row lock that each upsert
           would grab into one explicit up-front lock acquisition — so the
           transaction either gets everything it needs immediately or blocks
           until it can, rather than grabbing locks piecemeal and racing.
           Pure inserts (rows that don't exist yet) are unaffected.
        """
        with _db() as conn:
            with conn.cursor() as cur:
                # Take the session-wide write lock first so no other writer (the
                # dashboard's load/schema path, or a second engine instance) can
                # be inside a conflicting transaction. Auto-released on commit.
                cur.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_LOCK_KEY,))

                # ── state_meta (single-row upsert — no ordering needed) ──────
                cur.execute(
                    """INSERT INTO state_meta (id, realized_pnl, shadow_pnl, day, day_start_pnl, trading_days)
                       VALUES (1, %s, %s, %s, %s, %s)
                       ON CONFLICT (id) DO UPDATE
                         SET realized_pnl=EXCLUDED.realized_pnl,
                             shadow_pnl=EXCLUDED.shadow_pnl,
                             day=EXCLUDED.day,
                             day_start_pnl=EXCLUDED.day_start_pnl,
                             trading_days=EXCLUDED.trading_days""",
                    (self.realized_pnl_usd, self.shadow_pnl_usd, self.day, self.day_start_pnl,
                     ",".join(sorted(self.trading_days))),
                )

                # ── positions: sort → lock existing rows → upsert ────────────
                sorted_pos = sorted(
                    self.positions,
                    key=lambda p: (p.symbol, p.opened_at, p.shadow),
                )

                if sorted_pos:
                    # Pre-acquire RowShareLock on every row that already exists
                    # in the table, in the same deterministic order we will
                    # write them.  New rows (pure INSERTs) have no tuple yet
                    # and are therefore unaffected by this SELECT.
                    ph = ",".join(["(%s,%s,%s)"] * len(sorted_pos))
                    lock_vals: list = []
                    for p in sorted_pos:
                        lock_vals.extend([p.symbol, p.opened_at, p.shadow])
                    cur.execute(
                        f"SELECT 1 FROM positions "
                        f"WHERE (symbol, opened_at, shadow) IN ({ph}) "
                        f"ORDER BY symbol, opened_at, shadow "
                        f"FOR UPDATE",
                        lock_vals,
                    )

                for p in sorted_pos:
                    cur.execute(
                        """INSERT INTO positions (
                               symbol, asset, side, qty, entry_price, size_usd, stop,
                               target, kind, thesis, opened_at, mode, shadow,
                               open, exit_price, closed_at, pnl_usd, order_id, filled, contract
                           ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (symbol, opened_at, shadow) DO UPDATE
                             SET open=EXCLUDED.open, exit_price=EXCLUDED.exit_price,
                                 closed_at=EXCLUDED.closed_at, pnl_usd=EXCLUDED.pnl_usd,
                                 stop=EXCLUDED.stop, target=EXCLUDED.target,
                                 entry_price=EXCLUDED.entry_price, size_usd=EXCLUDED.size_usd,
                                 filled=EXCLUDED.filled""",
                        (p.symbol, p.asset, p.side, p.qty, p.entry_price, p.size_usd,
                         p.stop, p.target, p.kind, p.thesis, p.opened_at, p.mode,
                         p.shadow, p.open, p.exit_price, p.closed_at, p.pnl_usd,
                         p.order_id, p.filled, p.contract),
                    )

    def _roll_day(self) -> None:
        today = _today()
        if self.day != today:
            self.day = today
            self.day_start_pnl = self.realized_pnl_usd

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.open and not p.shadow]

    @property
    def open_shadow(self) -> list[Position]:
        return [p for p in self.positions if p.open and p.shadow]

    def has_open(self, symbol: str, shadow: bool = False) -> bool:
        return any(p.open and p.symbol == symbol and p.shadow == shadow for p in self.positions)

    def daily_pnl(self) -> float:
        return self.realized_pnl_usd - self.day_start_pnl

    def trade_stats(self) -> tuple[float, float, int]:
        """Win-rate p and payout ratio b from closed real (non-shadow) trades.

        Returns (p, b, n): p = wins/n, b = avg_win / avg_loss (both magnitudes).
        Break-even trades (pnl == 0) are excluded from the win/loss split but
        still counted in n so a churn of scratches doesn't fake an edge.
        b = 0.0 when there are no losing trades yet (caller must guard).
        """
        closed = [p for p in self.positions if not p.open and not p.shadow]
        n = len(closed)
        wins = [p.pnl_usd for p in closed if p.pnl_usd > 0]
        losses = [-p.pnl_usd for p in closed if p.pnl_usd < 0]
        if n == 0:
            return 0.0, 0.0, 0
        p = len(wins) / n
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        b = avg_win / avg_loss if avg_loss > 0 else 0.0
        return p, b, n

    def add(self, pos: Position) -> None:
        self.positions.append(pos)
        if not pos.shadow:
            self.trading_days.add(_topstep_session_date())

    def close(self, pos: Position, exit_price: float, pnl_override: float | None = None) -> None:
        pos.open = False
        pos.exit_price = exit_price
        pos.closed_at = _now()
        if pnl_override is not None:
            pos.pnl_usd = pnl_override     # options: premium-based P&L from the executor
        else:
            # long: (exit-entry)*qty ; short: (entry-exit)*qty, scaled to DOLLARS by
            # the futures contract multiplier ($/point). dollar_value_per_point returns
            # 1.0 for non-futures roots, so equities/options book unchanged. Without
            # this, ES P&L is booked in points (a 50× understatement) and the Topstep
            # daily-loss / trailing-MLL guards key off numbers that never breach.
            from futures_symbols import dollar_value_per_point
            direction = 1.0 if pos.side == "BUY" else -1.0
            mult = dollar_value_per_point(pos.symbol)
            pos.pnl_usd = (exit_price - pos.entry_price) * pos.qty * direction * mult
        if pos.shadow:
            self.shadow_pnl_usd += pos.pnl_usd
        else:
            self.realized_pnl_usd += pos.pnl_usd


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _topstep_session_date() -> str:
    """Date of the current CME/Topstep trading session (rolls 18:00 ET),
    matching Engine._topstep_session_date — used to count distinct active
    trading days toward the Combine's minimum-days requirement."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    d = now.date()
    if now.hour >= 18:
        d += timedelta(days=1)
    return d.isoformat()
