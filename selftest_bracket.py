"""One-shot SUPERVISED verification of ProjectX stopLossBracket semantics.

    .venv/bin/python selftest_bracket.py [SYMBOL]    # default MES

Places ONE 1-lot market entry with a server-side stopLossBracket attached
(the Phase C bracket path), adopts the bracket child via parentOrderId,
validates its side/price against the fill, then CANCELS the stop and
FLATTENS the position — total market exposure a few seconds, worst case one
spread + fees. Prints every step so the tick-sign convention (docs show
positive distances; the gateway derives direction) is confirmed empirically
before the engine's first unsupervised entry.

SAFETY RAILS — the script refuses to run when:
  * the broker is in mock mode (no creds / login failed),
  * PROJECTX_LIVE=true (funded env — this test is for sim/eval ONLY),
  * the kill switch is armed (disarm deliberately, run, re-arm),
  * a position in SYMBOL is already open on the account.

Exit codes: 0 bracket verified + flat; 1 refused/setup failure;
2 bracket wrong/missing (position flattened, engine bracket path must stay
   under review or PX_BRACKET_ENABLED=false);
3 CLEANUP FAILED — POSITION OR STOP MAY REST AT BROKER, INTERVENE MANUALLY.
"""
from __future__ import annotations

import sys
import time

from config import CONFIG
from futures_symbols import spec_for
from risk import kill_switch_active


def main() -> int:  # noqa: PLR0911, PLR0912, PLR0915 — linear supervised checklist
    sym = (sys.argv[1] if len(sys.argv) > 1 else "MES").upper()
    spec = spec_for(sym)
    if spec is None:
        print(f"✗ {sym} is not a known futures root")
        return 1
    if CONFIG.projectx_live:
        print("✗ PROJECTX_LIVE=true — refusing: this test is for sim/eval only")
        return 1
    if kill_switch_active():
        print("✗ kill switch ARMED — disarm deliberately for this test, re-arm after")
        return 1
    if not CONFIG.px_bracket_enabled:
        print("✗ PX_BRACKET_ENABLED=false — nothing to verify")
        return 1

    from projectx_executor import ProjectXBroker
    broker = ProjectXBroker()
    if getattr(broker, "_mock_mode", True):
        print("✗ broker in MOCK MODE — login failed or creds unset")
        return 1
    acct = broker.account()
    print(f"✓ connected | account={acct.get('account_id', '?')} "
          f"equity=${acct.get('equity', 0):,.2f} env=sim/eval")

    pre = [p for p in broker.get_positions()
           if broker.root_for_contract(str(p.get("contractId", ""))) == sym]
    if pre:
        print(f"✗ {sym} position already open on the account — resolve first")
        return 1

    bars = broker.historical_bars(sym, limit=3)
    closes = bars.get("close") or []
    if not closes:
        print("✗ no bars — market closed or feed issue")
        return 1
    ref = closes[-1]
    sl_ticks = int(round(10.0 / spec.tick_size))  # 10 points of stop distance
    print(f"→ placing 1x {sym} BUY @ ~{ref:.2f} with stopLossBracket ticks={sl_ticks} "
          f"(= {sl_ticks * spec.tick_size:g} pts, ${sl_ticks * spec.tick_value:,.0f}/contract)")

    fill = broker.submit(sym, 1, "BUY", ref, stop_loss_ticks=sl_ticks)
    print(f"✓ entry filled: order={fill.order_id} px={fill.price:.2f}")

    info = broker.find_bracket_stop(fill.order_id)
    verdict, code = "", 0
    if info is None:
        verdict, code = "✗ NO bracket child found — flattening", 2
    else:
        expect_px = fill.price - sl_ticks * spec.tick_size
        print(f"  child: order={info['order_id']} side={info['side']} "
              f"stop={info['stop_price']:.2f} size={info['size']} "
              f"(expected ≈{expect_px:.2f}, SELL, below fill {fill.price:.2f})")
        ok_side = info["side"] == "SELL"
        ok_px = 0 < info["stop_price"] < fill.price
        near = abs(info["stop_price"] - expect_px) <= 4 * spec.tick_size
        if ok_side and ok_px:
            verdict = ("✓ BRACKET VERIFIED: stop rests on the correct side"
                       + ("" if near else
                          " (⚠ distance differs from requested — check ticks math)"))
            code = 0 if near else 2
        else:
            verdict, code = ("✗ bracket child WRONG (side/price) — tick convention "
                             "misread; keep PX_BRACKET_ENABLED=false"), 2
    print(verdict)

    # ── cleanup: cancel stop, then flatten; verify actually flat ─────────────
    cleanup_ok = True
    if info and info.get("order_id"):
        cleanup_ok &= bool(broker.cancel_order(info["order_id"]))
        print(f"{'✓' if cleanup_ok else '✗'} stop child cancelled")
    try:
        res = broker.flatten_all()
        print(f"✓ flatten submitted: {res}")
    except Exception as exc:  # noqa: BLE001
        print(f"✗ flatten failed: {exc}")
        cleanup_ok = False
    time.sleep(1.5)
    left = [p for p in broker.get_positions()
            if broker.root_for_contract(str(p.get("contractId", ""))) == sym]
    if left:
        print(f"✗ STILL OPEN at broker: {left} — MANUAL INTERVENTION REQUIRED")
        return 3
    print("✓ broker confirms flat")
    if not cleanup_ok:
        print("⚠ a cancel/flatten step errored above — check working orders on the "
              "account for a leftover jarvis_* stop before trusting this run")
        return 3
    return code


if __name__ == "__main__":
    raise SystemExit(main())
