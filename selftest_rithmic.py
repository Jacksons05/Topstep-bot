"""Rithmic connection + L2 order-flow self-test.

    .venv/bin/python selftest_rithmic.py [SYMBOL]   # default MES

Connects with the .env credentials, prints login + account, subscribes the
symbol's BBO + LAST_TRADE + ORDER_BOOK, and after a few seconds reports live
OBI / micro-price / CVD / depth so you can confirm the feed (and the account's
CME Level-2 entitlement) before wiring it into the engine.

Exit codes: 0 = connected + data; 1 = connect/login failed; 2 = connected but
no market data (market closed, or symbol/exchange wrong).
"""
from __future__ import annotations

import sys
import time

from config import CONFIG
from futures_symbols import spec_for


def main() -> int:
    sym = (sys.argv[1] if len(sys.argv) > 1 else "MES").upper()
    spec = spec_for(sym)
    if spec is None:
        print(f"✗ {sym} is not a known futures root (futures_symbols.py)")
        return 1

    print(f"Rithmic self-test | system={CONFIG.rithmic_system} env={CONFIG.rithmic_env} "
          f"url={CONFIG.rithmic_url} app={CONFIG.rithmic_app_name}/{CONFIG.rithmic_app_version}")

    from rithmic_executor import RithmicBroker
    from rithmic_marketdata import RithmicOrderFlowFeed

    broker = RithmicBroker()
    if getattr(broker, "_mock_mode", True):
        print("✗ broker in MOCK MODE — connect/login failed (check creds, system, "
              "and RITHMIC_APP_NAME/VERSION; a permission-denied login usually = wrong app_name).")
        return 1

    try:
        acct = broker.account()
        print(f"✓ connected | account={acct.get('account_id','?')} "
              f"equity=${acct.get('equity',0):,.2f} source={acct.get('source')}")
    except Exception as e:  # noqa: BLE001
        print(f"✗ account fetch failed: {e}")
        broker.close()
        return 1

    feed = RithmicOrderFlowFeed(broker)
    n = feed.subscribe([sym])
    print(f"subscribed {n} root(s) | L2 depth_available={feed.depth_available}")

    eng = feed.get(sym)
    print("collecting 10s of market data...")
    got = False
    for _ in range(10):
        time.sleep(1)
        if eng.has_data:
            got = True
            snap = eng.snapshot()
            depth = "L2" if eng.book.has_depth else "BBO"
            print(f"  [{depth}] OBI={snap.obi:+.3f} micro={snap.micro_price:.2f} "
                  f"CVD={snap.cvd:+.0f} whale={snap.whale:+d} "
                  f"bid={eng.bid}@{eng.bid_size:g} ask={eng.ask}@{eng.ask_size:g}")

    broker.close()
    if not got:
        print("⚠ connected but no ticks — market may be closed, or the CME data "
              "subscription / symbol-exchange needs checking.")
        return 2
    print("✓ live order-flow confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
