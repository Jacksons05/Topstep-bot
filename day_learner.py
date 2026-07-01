"""End-of-day learning: translate today's closed trades into bounded config
adjustments for tomorrow.

Runs once at market close (engine.py calls trigger() on first closed-market
detection each day). Writes day_adapt.json next to this file. Both engines
read that file at startup and apply the adaptations as live overrides on top
of their static .env config.

Adaptations are CONSERVATIVE — small deltas bounded by MAX_* constants.
The goal is signal, not whiplash. Multiple bad days compound; one bad day
barely moves the needle.

Adaptation surfaces:
  confidence_threshold_adj   +/- 0.05 based on day precision vs baseline
  side_size  BUY / SELL      0.5-1.0 multiplier if one side underperformed
  regime_size  {regime: mult}  per-regime size multiplier for underperformers
  hour_block  list of ET hours to skip (e.g. ["09"] = no 9:xx trades)
  symbol_cooldown  list of symbols with 3+ consecutive losses today
  cramer_flip_enabled  bool — turn on signal inversion if shadow crushed real

The engine applies these by modifying in-memory CONFIG attributes at startup;
the .env file is never touched, so a restart always starts from a clean base.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("day_learner")
ROOT = Path(__file__).resolve().parent
ADAPT_PATH = ROOT / "day_adapt.json"

# ── adaptation bounds ─────────────────────────────────────────────────────
MAX_CONF_ADJ      = 0.05   # max daily confidence threshold shift
MAX_SIZE_HAIRCUT  = 0.50   # minimum size multiplier from learning (never below 50%)
MIN_TRADES_REGIME = 3      # minimum trades before a regime weight is adjusted
MIN_TRADES_HOUR   = 2      # minimum trades in an hour before blocking it
CONSEC_LOSS_BLOCK = 3      # consecutive losses to trigger symbol cooldown


@dataclass
class DayStats:
    date: str = ""
    bot: str = ""
    n_trades: int = 0
    n_wins: int = 0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    shadow_pnl: float = 0.0       # how much the inverse book made (Cramer edge)
    by_regime: dict = field(default_factory=dict)
    by_side: dict = field(default_factory=dict)
    by_hour: dict = field(default_factory=dict)
    by_confidence: dict = field(default_factory=dict)
    symbol_streaks: dict = field(default_factory=dict)  # symbol → consecutive loss count


@dataclass
class DayAdapt:
    date: str = ""
    bot: str = ""
    stats: dict = field(default_factory=dict)
    # ── applied adjustments ───────────────────────────────────────────────
    confidence_threshold_adj: float = 0.0   # added to CONFIG.confidence_threshold
    side_size: dict = field(default_factory=dict)   # {"BUY": 1.0, "SELL": 0.7}
    regime_size: dict = field(default_factory=dict) # {"negative-gamma": 0.6}
    hour_block: list = field(default_factory=list)  # ["09"]  → skip 9:xx ET
    symbol_cooldown: list = field(default_factory=list)
    cramer_flip_enabled: bool = False
    reasoning: list = field(default_factory=list)   # human-readable explanation lines


# ── SQLite reader (JARVIS) ────────────────────────────────────────────────

def _load_sqlite(db_path: str, lookback_days: int = 1) -> list[dict]:
    """Load today's closed real positions + matching decisions from SQLite."""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        cutoff = f"{date.today()}"
        rows = conn.execute("""
            SELECT p.symbol, p.side, p.pnl_usd, p.opened_at, p.closed_at,
                   p.kind,
                   COALESCE(d.regime, '') AS regime,
                   COALESCE(d.quant_lean, 0) AS quant_lean,
                   COALESCE(d.qual_lean, 0) AS qual_lean,
                   s.pnl_usd AS shadow_pnl
            FROM positions p
            LEFT JOIN decisions d ON d.symbol = p.symbol AND d.opened_at = p.opened_at
            LEFT JOIN positions s ON s.symbol = p.symbol AND s.shadow = 1
                AND CAST(strftime('%s', s.opened_at) AS INTEGER)
                    BETWEEN CAST(strftime('%s', p.opened_at) AS INTEGER) - 2
                        AND CAST(strftime('%s', p.opened_at) AS INTEGER) + 2
            WHERE p.shadow = 0
              AND p.open   = 0
              AND p.closed_at IS NOT NULL
              AND date(p.closed_at) >= ?
            ORDER BY p.closed_at ASC
        """, (cutoff,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log.warning(f"day_learner sqlite load failed: {e}")
        return []


def _load_postgres(db_url: str, lookback_days: int = 1) -> list[dict]:
    """Load today's closed real positions from Postgres (Topstep)."""
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(db_url)
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cutoff = str(date.today())
        cur.execute("""
            SELECT p.symbol, p.side, p.pnl_usd, p.opened_at, p.closed_at,
                   p.kind,
                   '' AS regime, 0.0 AS quant_lean, 0.0 AS qual_lean,
                   s.pnl_usd AS shadow_pnl
            FROM positions p
            LEFT JOIN positions s ON s.symbol = p.symbol AND s.shadow = TRUE
                AND s.opened_at BETWEEN
                    to_char(to_timestamp(p.opened_at, 'YYYY-MM-DD"T"HH24:MI:SS')
                            - INTERVAL '2 seconds', 'YYYY-MM-DD"T"HH24:MI:SS')
                AND to_char(to_timestamp(p.opened_at, 'YYYY-MM-DD"T"HH24:MI:SS')
                            + INTERVAL '2 seconds', 'YYYY-MM-DD"T"HH24:MI:SS')
            WHERE p.shadow = FALSE
              AND p.open   = FALSE
              AND p.closed_at IS NOT NULL
              AND left(p.closed_at, 10) >= %s
            ORDER BY p.closed_at ASC
        """, (cutoff,))
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        conn.close()
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning(f"day_learner postgres load failed: {e}")
        return []


# ── analysis ──────────────────────────────────────────────────────────────

def _hour_et(ts_str: str) -> str:
    """Extract 2-digit hour string in ET from an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        # Convert UTC to ET (UTC-4 EDT, UTC-5 EST; approximate with -4 in summer)
        et_hour = (dt.hour - 4) % 24
        return f"{et_hour:02d}"
    except Exception:  # noqa: BLE001
        return "??"


def _conf_bucket(quant_lean: float, qual_lean: float) -> str:
    conf = 0.5 * abs(quant_lean) + 0.5 * abs(qual_lean)
    if conf < 0.72:
        return "0.70-0.72"
    if conf < 0.75:
        return "0.72-0.75"
    if conf < 0.80:
        return "0.75-0.80"
    return "0.80+"


def _analyze(trades: list[dict], bot: str) -> DayStats:
    stats = DayStats(date=str(date.today()), bot=bot)
    stats.n_trades = len(trades)
    if not trades:
        return stats

    wins = [t for t in trades if (t["pnl_usd"] or 0) > 0]
    losses = [t for t in trades if (t["pnl_usd"] or 0) < 0]
    stats.n_wins = len(wins)
    stats.total_pnl = sum(t["pnl_usd"] or 0 for t in trades)
    stats.shadow_pnl = sum(t["shadow_pnl"] or 0 for t in trades if t.get("shadow_pnl"))
    stats.avg_win = (sum(t["pnl_usd"] for t in wins) / len(wins)) if wins else 0.0
    stats.avg_loss = (sum(t["pnl_usd"] for t in losses) / len(losses)) if losses else 0.0

    # by regime
    regime_g: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        r = t.get("regime") or "unknown"
        regime_g[r].append(t["pnl_usd"] or 0)
    for r, pnls in regime_g.items():
        wins_r = sum(1 for p in pnls if p > 0)
        stats.by_regime[r] = {"n": len(pnls), "win_rate": wins_r / len(pnls),
                               "pnl": round(sum(pnls), 2)}

    # by side
    side_g: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        side_g[t["side"]].append(t["pnl_usd"] or 0)
    for side, pnls in side_g.items():
        wins_s = sum(1 for p in pnls if p > 0)
        stats.by_side[side] = {"n": len(pnls), "win_rate": wins_s / len(pnls),
                                "pnl": round(sum(pnls), 2)}

    # by hour
    hour_g: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        h = _hour_et(t.get("closed_at") or "")
        if h != "??":
            hour_g[h].append(t["pnl_usd"] or 0)
    for h, pnls in hour_g.items():
        stats.by_hour[h] = {"n": len(pnls), "pnl": round(sum(pnls), 2)}

    # by confidence bucket
    conf_g: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        b = _conf_bucket(t.get("quant_lean") or 0, t.get("qual_lean") or 0)
        conf_g[b].append(t["pnl_usd"] or 0)
    for b, pnls in conf_g.items():
        wins_c = sum(1 for p in pnls if p > 0)
        stats.by_confidence[b] = {"n": len(pnls), "win_rate": wins_c / len(pnls)}

    # symbol consecutive losses
    sym_streaks: dict[str, int] = defaultdict(int)
    for t in sorted(trades, key=lambda x: x.get("closed_at") or ""):
        sym = t["symbol"]
        if (t["pnl_usd"] or 0) < 0:
            sym_streaks[sym] += 1
        else:
            sym_streaks[sym] = 0
    stats.symbol_streaks = dict(sym_streaks)

    return stats


def _build_adapt(stats: DayStats, prev_adapt: DayAdapt | None, cfg) -> DayAdapt:
    """Derive tomorrow's adaptation from today's stats.

    cfg is a simple namespace with fields: confidence_threshold, cramer_flip_threshold_usd
    (or None if unavailable). Adjustments are bounded + Bayesian-shrunk.
    """
    adapt = DayAdapt(date=str(date.today()), bot=stats.bot)
    adapt.stats = asdict(stats)
    reasons: list[str] = []

    if stats.n_trades < 3:
        reasons.append(f"only {stats.n_trades} trades — too few to learn from; no adaptation")
        adapt.reasoning = reasons
        return adapt

    win_rate = stats.n_wins / stats.n_trades if stats.n_trades else 0.5
    cramer_edge = stats.shadow_pnl - stats.total_pnl  # positive = shadow won

    # ── 1. Confidence threshold ───────────────────────────────────────────
    # Win rate below 40% → tighten threshold (raise by up to MAX_CONF_ADJ)
    # Win rate above 60% → loosen (lower by up to MAX_CONF_ADJ)
    # Shrink by n/(n+10) so few trades barely move it
    shrink = stats.n_trades / (stats.n_trades + 10)
    if win_rate < 0.40:
        raw_adj = MAX_CONF_ADJ * (0.40 - win_rate) / 0.40
        adj = round(raw_adj * shrink, 3)
        adapt.confidence_threshold_adj = adj
        reasons.append(f"win_rate={win_rate:.0%} < 40% → conf_threshold +{adj:.3f} tomorrow")
    elif win_rate > 0.60:
        raw_adj = MAX_CONF_ADJ * (win_rate - 0.60) / 0.40
        adj = round(raw_adj * shrink, 3)
        adapt.confidence_threshold_adj = -adj
        reasons.append(f"win_rate={win_rate:.0%} > 60% → conf_threshold -{adj:.3f} (loosen)")

    # ── 2. Side size multipliers ──────────────────────────────────────────
    for side, sdata in stats.by_side.items():
        if sdata["n"] < 2:
            continue
        swr = sdata["win_rate"]
        if swr < 0.35:
            mult = max(MAX_SIZE_HAIRCUT, round(swr / 0.50, 2))
            adapt.side_size[side] = mult
            reasons.append(f"{side} win_rate={swr:.0%} → size ×{mult:.2f} tomorrow")
        elif swr > 0.65 and side not in adapt.side_size:
            adapt.side_size[side] = 1.0   # explicit "no haircut" so loader knows

    # ── 3. Regime size multipliers ────────────────────────────────────────
    for regime, rdata in stats.by_regime.items():
        if regime in ("unknown", "") or rdata["n"] < MIN_TRADES_REGIME:
            continue
        rwr = rdata["win_rate"]
        if rwr < 0.33:
            mult = max(MAX_SIZE_HAIRCUT, round(rwr / 0.50, 2))
            adapt.regime_size[regime] = mult
            reasons.append(f"regime={regime} win_rate={rwr:.0%} ({rdata['n']} trades) "
                           f"→ size ×{mult:.2f}")

    # ── 4. Hour blocking ──────────────────────────────────────────────────
    for hour, hdata in stats.by_hour.items():
        if hdata["n"] < MIN_TRADES_HOUR:
            continue
        if hdata["pnl"] < -abs(stats.avg_loss) * hdata["n"] * 0.8:
            adapt.hour_block.append(hour)
            reasons.append(f"hour {hour}:xx ET all-negative (pnl={hdata['pnl']:.0f}, "
                           f"n={hdata['n']}) → block tomorrow")

    # ── 5. Symbol cooldowns ───────────────────────────────────────────────
    for sym, streak in stats.symbol_streaks.items():
        if streak >= CONSEC_LOSS_BLOCK:
            adapt.symbol_cooldown.append(sym)
            reasons.append(f"{sym}: {streak} consecutive losses today → 1-day cooldown")

    # ── 6. Cramer flip ───────────────────────────────────────────────────
    # Shadow book beat real by > $500 AND real was negative → consider enabling flip
    base_flip_thresh = getattr(cfg, "cramer_flip_threshold_usd", 1000.0)
    if cramer_edge > base_flip_thresh * 0.5 and stats.total_pnl < 0:
        adapt.cramer_flip_enabled = True
        reasons.append(f"shadow beat real by ${cramer_edge:.0f} with negative day "
                       f"→ CRAMER_FLIP_ENABLED tomorrow (mirror signals)")
    else:
        adapt.cramer_flip_enabled = False

    adapt.reasoning = reasons
    return adapt


# ── public API ────────────────────────────────────────────────────────────

_trigger_lock = threading.Lock()
_triggered_today: set[str] = set()


def trigger(bot: str, cfg=None, also_retrain: bool = False) -> DayAdapt | None:
    """Called by the engine on first market-close detection each day.

    bot: "jarvis" or "topstep"
    cfg: the engine's CONFIG (or None for defaults)
    also_retrain: if True, kick off retrain+promote pipeline (JARVIS only)

    Thread-safe: if called twice in the same process on the same day, the
    second call is a no-op so idle-scan loops don't re-trigger.
    """
    today = str(date.today())
    key = f"{bot}:{today}"
    with _trigger_lock:
        if key in _triggered_today:
            return None
        _triggered_today.add(key)

    log.info(f"[day_learner] EOD learning triggered for {bot} ({today})")

    # load trades
    db_url = os.getenv("DATABASE_URL", "")
    db_path = os.getenv("STATE_DB_PATH", "state.db")
    if db_url and "postgres" in db_url:
        trades = _load_postgres(db_url)
    else:
        trades = _load_sqlite(db_path)

    stats = _analyze(trades, bot)
    log.info(f"[day_learner] {stats.n_trades} closed trades | "
             f"win_rate={stats.n_wins}/{stats.n_trades} | pnl=${stats.total_pnl:.2f}")

    # load previous adapt for carry-forward logic (future: accumulate multi-day)
    prev = load_adapt()

    adapt = _build_adapt(stats, prev, cfg or _DummyCfg())
    _save(adapt)

    for r in adapt.reasoning:
        log.info(f"[day_learner] {r}")

    if also_retrain:
        _spawn_retrain()

    return adapt


def load_adapt() -> DayAdapt | None:
    """Read day_adapt.json from disk. Returns None if absent or from a prior day."""
    if not ADAPT_PATH.exists():
        return None
    try:
        raw = json.loads(ADAPT_PATH.read_text())
        if raw.get("date") != str(date.today()):
            return None   # stale — prior day's file
        a = DayAdapt()
        for k, v in raw.items():
            if hasattr(a, k):
                setattr(a, k, v)
        return a
    except Exception:  # noqa: BLE001
        return None


def apply_to_config(cfg, adapt: DayAdapt | None, notify_fn=None) -> None:
    """Mutate CONFIG in-place to apply today's adaptations at engine startup.

    Called once at engine init after State.load(). safe to call with adapt=None.
    """
    if adapt is None or not adapt.reasoning:
        return

    def note(msg: str) -> None:
        log.info(f"[day_learner] applied: {msg}")
        if notify_fn:
            notify_fn(f"[DayAdapt] {msg}")

    def _setcfg(key: str, val: Any) -> None:
        """Set an attribute on cfg, bypassing frozen-dataclass __setattr__ if needed."""
        try:
            object.__setattr__(cfg, key, val)
        except (AttributeError, TypeError):
            try:
                setattr(cfg, key, val)
            except Exception:  # noqa: BLE001
                pass  # give up gracefully — adaptation degrades but won't crash

    # 1. Confidence threshold
    if adapt.confidence_threshold_adj != 0:
        old = cfg.confidence_threshold
        new_thresh = round(max(0.55, min(0.90, old + adapt.confidence_threshold_adj)), 3)
        _setcfg("confidence_threshold", new_thresh)
        note(f"conf_threshold {old:.3f} → {new_thresh:.3f} "
             f"(adj={adapt.confidence_threshold_adj:+.3f})")

    # 2. Side multipliers — stored on cfg for the engine to check at sizing time
    if adapt.side_size:
        _setcfg("_day_side_size", adapt.side_size)
        note(f"side_size: {adapt.side_size}")

    # 3. Regime size multipliers
    if adapt.regime_size:
        _setcfg("_day_regime_size", adapt.regime_size)
        note(f"regime_size: {adapt.regime_size}")

    # 4. Hour blocks
    if adapt.hour_block:
        _setcfg("_day_hour_block", set(adapt.hour_block))
        note(f"hour_block: {adapt.hour_block}")

    # 5. Symbol cooldowns — engine skip-list for new entries
    if adapt.symbol_cooldown:
        _setcfg("_day_symbol_cooldown", set(adapt.symbol_cooldown))
        note(f"symbol_cooldown: {adapt.symbol_cooldown}")

    # 6. Cramer flip
    if adapt.cramer_flip_enabled and not cfg.cramer_flip_enabled:
        _setcfg("cramer_flip_enabled", True)
        note("CRAMER_FLIP_ENABLED → True (shadow beat real yesterday)")


# ── internals ─────────────────────────────────────────────────────────────

def _save(adapt: DayAdapt) -> None:
    ADAPT_PATH.write_text(json.dumps(asdict(adapt), indent=2))
    log.info(f"[day_learner] wrote {ADAPT_PATH}")


def _spawn_retrain() -> None:
    """Fire retrain_and_promote.sh in the background (non-blocking)."""
    import subprocess
    script = ROOT / "retrain_and_promote.sh"
    if not script.exists():
        log.warning("[day_learner] retrain_and_promote.sh not found — skipping retrain")
        return
    try:
        subprocess.Popen(
            ["bash", str(script)],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("[day_learner] retrain_and_promote.sh spawned in background")
    except Exception as e:  # noqa: BLE001
        log.warning(f"[day_learner] could not spawn retrain: {e}")


class _DummyCfg:
    """Fallback when cfg is not passed — provides sentinel attribute access."""
    confidence_threshold = 0.70
    cramer_flip_enabled = False
    cramer_flip_threshold_usd = 1000.0
