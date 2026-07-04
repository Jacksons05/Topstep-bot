"""Trading status — the runtime counterpart to preflight.py.

Read-only snapshot of the book: open positions, realized / day P&L, the Cramer
shadow book, and Topstep risk headroom (trailing-MLL floor, Daily Loss Limit,
account-wide contract cap). It NEVER places, cancels, or modifies an order.

    python trading_status.py           # human-readable
    python trading_status.py --json    # machine-readable

State is read from the same store the engine writes to (Postgres when
DATABASE_URL is set, otherwise an empty in-memory book). P&L figures are
book-based (realized + recorded position P&L); unrealized mark-to-market needs a
live feed and is only shown when a broker balance is available.

Exit code is always 0 — this is informational, not a gate (use preflight.py for
the go/no-go check).
"""
from __future__ import annotations

import json
import sys

from config import CONFIG


def _gather() -> dict:
    from state import State
    from topstep_risk import TopstepRiskManager

    st = State.load()
    open_real = list(st.open_positions)     # property: real book only
    open_shadow = list(st.open_shadow)
    contracts = sum(int(p.qty) for p in open_real)
    realized = st.realized_pnl_usd
    day_pnl = st.daily_pnl()
    shadow = st.shadow_pnl_usd

    # Book-based equity proxy (no live marks here): account start + realized.
    equity = CONFIG.topstep_account_size + realized
    ts = TopstepRiskManager(initial_equity=equity)
    floor = ts.mll_floor()

    try:
        from risk import kill_switch_active
        killed = kill_switch_active()
    except Exception:  # noqa: BLE001
        killed = None

    return {
        "mode": "live" if CONFIG.is_live else "paper",
        "broker": CONFIG.broker,
        "kill_switch": killed,
        "stateful": bool(getattr(__import__("state"), "DATABASE_URL", "")),
        "positions": [
            {
                "symbol": p.symbol, "side": p.side, "qty": int(p.qty),
                "entry": p.entry_price, "stop": p.stop, "target": p.target,
                "opened_at": p.opened_at,
            }
            for p in open_real
        ],
        "shadow_positions": len(open_shadow),
        "contracts_open": contracts,
        "contracts_cap": CONFIG.topstep_max_contracts,
        "realized_pnl": realized,
        "day_pnl": day_pnl,
        "shadow_pnl": shadow,
        "equity_proxy": equity,
        "mll_floor": floor,
        "mll_headroom": equity - floor,
        "daily_loss_limit": CONFIG.topstep_daily_loss_limit,
        "dll_used": min(0.0, day_pnl),
        "consistency_cap": CONFIG.topstep_consistency_pct * CONFIG.topstep_profit_target,
    }


def _render(s: dict) -> str:
    L = ["", "═══ JARVIS Topstep — trading status ═══", ""]
    L.append(f"  mode={s['mode']} | broker={s['broker']} | "
             f"state={'Postgres' if s['stateful'] else 'in-memory (stateless)'} | "
             f"kill_switch={'ARMED' if s['kill_switch'] else 'clear' if s['kill_switch'] is not None else '?'}")
    L.append("")

    # Positions
    if s["positions"]:
        L.append(f"  Open positions ({s['contracts_open']}/{s['contracts_cap']} contracts):")
        L.append(f"    {'SYM':<5} {'SIDE':<5} {'QTY':>3}  {'ENTRY':>10} {'STOP':>10} {'TARGET':>10}")
        for p in s["positions"]:
            L.append(f"    {p['symbol']:<5} {p['side']:<5} {p['qty']:>3}  "
                     f"{p['entry']:>10.2f} {p['stop']:>10.2f} {p['target']:>10.2f}")
    else:
        L.append(f"  Open positions: none ({s['contracts_open']}/{s['contracts_cap']} contracts)")
    if s["shadow_positions"]:
        L.append(f"  Cramer shadow book: {s['shadow_positions']} open")
    L.append("")

    # P&L
    L.append("  P&L:")
    L.append(f"    realized      ${s['realized_pnl']:>12,.2f}")
    L.append(f"    today         ${s['day_pnl']:>12,.2f}")
    L.append(f"    cramer shadow ${s['shadow_pnl']:>12,.2f}")
    L.append("")

    # Topstep risk
    L.append("  Topstep risk (book-based; unrealized needs a live feed):")
    L.append(f"    equity (proxy)     ${s['equity_proxy']:>12,.2f}")
    L.append(f"    trailing-MLL floor ${s['mll_floor']:>12,.2f}")
    head = s["mll_headroom"]
    flag = "  ⚠ THIN" if 0 < head < s["daily_loss_limit"] else ("  ✗ BREACHED" if head <= 0 else "")
    L.append(f"    headroom to floor  ${head:>12,.2f}{flag}")
    L.append(f"    daily loss limit   ${s['daily_loss_limit']:>12,.0f}  (used ${abs(s['dll_used']):,.2f})")
    L.append(f"    consistency cap    ${s['consistency_cap']:>12,.0f}  (per-day profit stop)")
    L.append("")
    return "\n".join(L)


def main() -> int:
    # State.load() prints an advisory to stdout when DATABASE_URL is unset; route
    # it to stderr so stdout carries only the report / JSON payload.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        s = _gather()
    finally:
        sys.stdout = real_stdout
    if "--json" in sys.argv:
        print(json.dumps(s, indent=2, default=str))
    else:
        print(_render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
