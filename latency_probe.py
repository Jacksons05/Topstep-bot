"""LIVE, READ-ONLY tick-latency / granularity probe (Rule-1 inventory).

Opens a SECOND SignalR market-hub connection (market data only — NEVER places
or touches orders), subscribes ES quotes+trades+depth, and measures for a fixed
window:
  * inter-arrival time per stream (update granularity — clock-skew-free)
  * depth ladder size per GatewayDepth message
  * IF the payload carries a server timestamp: apparent end-to-end latency
    (recv_wallclock - server_ts). NOTE: subject to WSL<->exchange clock skew;
    the MIN bounds (true_latency + skew), so min is the informative floor.

Mirrors projectx_marketdata._connect exactly (proven API). Safe to run while the
bot soaks flat with ENTRY_ENGINE=off. Not research (Rule 0) — a live measurement.

Usage: .venv/bin/python latency_probe.py [SECONDS] [SYMBOL]
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict

from config import CONFIG
from projectx_executor import ProjectXBroker
from signalrcore.hub_connection_builder import HubConnectionBuilder

WINDOW = int(sys.argv[1]) if len(sys.argv) > 1 else 75
WANT = (sys.argv[2] if len(sys.argv) > 2 else "ES").upper()

recv = defaultdict(list)          # stream -> [perf_counter recv times]
first_payload = {}               # stream -> raw dict (for key discovery)
depth_levels = []                # entries per GatewayDepth message
lat_ms = defaultdict(list)       # stream -> [apparent latency ms] if server ts found
_ts_field = {}                   # stream -> (fieldname, kind)


def _find_server_ts(d: dict):
    """Return (fieldname, parsed_epoch_seconds) if a timestamp-like field parses."""
    for k, v in d.items():
        if not any(s in k.lower() for s in ("time", "ts", "stamp")):
            continue
        try:
            if isinstance(v, str):
                from datetime import datetime
                ep = datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                return k, ep
            if isinstance(v, (int, float)):
                fv = float(v)
                # heuristic: ms vs s epoch
                if fv > 1e12:
                    return k, fv / 1000.0
                if fv > 1e9:
                    return k, fv
        except Exception:
            continue
    return None, None


def _handler(stream):
    def h(args):
        now_pc = time.perf_counter()
        now_wall = time.time()
        recv[stream].append(now_pc)
        payload = args[1] if len(args) > 1 else None
        items = payload if isinstance(payload, list) else [payload]
        if stream == "depth" and isinstance(payload, list):
            depth_levels.append(len(payload))
        for it in items:
            if not isinstance(it, dict):
                continue
            if stream not in first_payload:
                first_payload[stream] = dict(it)
                fld, ep = _find_server_ts(it)
                if fld:
                    _ts_field[stream] = fld
            fld = _ts_field.get(stream)
            if fld and fld in it:
                _, ep = _find_server_ts({fld: it[fld]})
                if ep:
                    lat_ms[stream].append((now_wall - ep) * 1000.0)
    return h


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, int(p / 100.0 * len(xs)))
    return xs[i]


def main():
    print(f"[probe] authenticating broker (read-only)...")
    br = ProjectXBroker()
    if getattr(br, "_mock_mode", True) or not getattr(br, "token", ""):
        print("[probe] ABORT: broker has no live token (mock mode / creds).")
        return 1
    cid = None
    for sym in (WANT, "MES", "MNQ", "ES"):
        cid = br.contract_id(sym)
        if cid:
            print(f"[probe] resolved {sym} -> contractId {cid}")
            break
    if not cid:
        print("[probe] ABORT: could not resolve any contractId.")
        return 1

    url = f"{CONFIG.projectx_rtc_base.rstrip('/')}/hubs/market?access_token={br.token}"
    conn = (HubConnectionBuilder()
            .with_url(url, options={"skip_negotiation": True})
            .with_automatic_reconnect({"type": "raw", "keep_alive_interval": 5,
                                       "reconnect_interval": 2})
            .build())
    conn.on("GatewayQuote", _handler("quote"))
    conn.on("GatewayTrade", _handler("trade"))
    conn.on("GatewayDepth", _handler("depth"))
    conn.start()
    time.sleep(1.5)
    conn.send("SubscribeContractQuotes", [cid])
    conn.send("SubscribeContractTrades", [cid])
    conn.send("SubscribeContractMarketDepth", [cid])
    print(f"[probe] subscribed; capturing {WINDOW}s ...")

    t0 = time.time()
    last_report = 0
    while time.time() - t0 < WINDOW:
        time.sleep(2.0)
        el = int(time.time() - t0)
        if el - last_report >= 15:
            last_report = el
            print(f"  {el:>3}s  quotes={len(recv['quote'])} "
                  f"trades={len(recv['trade'])} depth={len(recv['depth'])}")
    try:
        conn.stop()
    except Exception:
        pass

    dur = time.time() - t0
    print("\n" + "=" * 70)
    print(f"  ES/MES tick capture — {dur:.0f}s, contract {cid}")
    print("=" * 70)
    for stream in ("quote", "trade", "depth"):
        pcs = recv[stream]
        n = len(pcs)
        rate = n / dur if dur else 0
        print(f"\n[{stream}]  n={n}  rate={rate:.1f}/s")
        if n >= 2:
            gaps = [(pcs[i] - pcs[i - 1]) * 1000 for i in range(1, n)]
            print(f"   inter-arrival ms: p50={pct(gaps,50):.1f}  "
                  f"p95={pct(gaps,95):.1f}  p99={pct(gaps,99):.1f}  "
                  f"max={max(gaps):.0f}")
        if stream == "depth" and depth_levels:
            print(f"   depth entries/msg: p50={pct(depth_levels,50):.0f}  "
                  f"max={max(depth_levels)}")
        if first_payload.get(stream):
            keys = list(first_payload[stream].keys())
            print(f"   payload keys: {keys}")
            print(f"   sample: {json.dumps(first_payload[stream])[:240]}")
        if lat_ms.get(stream):
            L = lat_ms[stream]
            print(f"   APPARENT latency ms (server_ts field '{_ts_field[stream]}'): "
                  f"min={min(L):.0f}  p50={pct(L,50):.0f}  p95={pct(L,95):.0f}  "
                  f"(NOTE: incl. WSL<->exchange clock skew; min is the floor)")
        elif first_payload.get(stream):
            print("   (no server-timestamp field found -> true latency NOT "
                  "measurable, only inter-arrival granularity above)")
    print("\n[probe] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
