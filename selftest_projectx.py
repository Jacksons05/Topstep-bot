"""ProjectX (TopstepX) connection + order-flow self-test.

    .venv/bin/python selftest_projectx.py [SYMBOL]   # default MES

Connects with the .env credentials, prints login + account, resolves the
contract, subscribes the symbol's GatewayQuote + GatewayTrade + GatewayDepth on
the SignalR market hub, and after a few seconds reports live OBI / micro-price /
CVD / depth so you can confirm the feed (and the account's L2 entitlement)
before wiring it into the engine.

Requires `signalrcore` in the venv for the live feed (pip install signalrcore).

Exit codes: 0 = connected + data; 1 = connect/login failed; 2 = connected but
no market data (market closed, or symbol/contract wrong, or signalrcore missing).
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

    print(f"ProjectX self-test | base={CONFIG.projectx_api_base} "
          f"rtc={CONFIG.projectx_rtc_base} live={CONFIG.projectx_live} "
          f"user={CONFIG.projectx_username or '(unset)'}")

    from projectx_executor import ProjectXBroker
    from projectx_marketdata import ProjectXOrderFlowFeed

    broker = ProjectXBroker()
    if getattr(broker, "_mock_mode", True):
        print("✗ broker in MOCK MODE — login failed or creds unset "
              "(check PROJECTX_USERNAME / PROJECTX_API_KEY).")
        return 1

    try:
        acct = broker.account()
        print(f"✓ connected | account={acct.get('account_id','?')} "
              f"equity=${acct.get('equity',0):,.2f} source={acct.get('source')}")
    except Exception as e:  # noqa: BLE001
        print(f"✗ account fetch failed: {e}")
        broker.close()
        return 1

    cid = broker.contract_id(sym)
    print(f"contract: {sym} -> {cid}")

    feed = ProjectXOrderFlowFeed(broker)
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

    feed.close()
    broker.close()
    if not got:
        print("⚠ connected but no ticks — market may be closed, the contract/symbol "
              "needs checking, or signalrcore is not installed (pip install signalrcore).")
        return 2
    print("✓ live order-flow confirmed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
