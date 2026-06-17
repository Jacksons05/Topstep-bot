"""JARVIS — local command center for the trading engine.

Zero external deps beyond what the bot already uses. Pure stdlib http.server.
Reads the same DB/log the engine writes, so it works locally (stateless +
signals.log) or against Railway Postgres (DATABASE_URL set in .env).

    python dashboard.py            # http://127.0.0.1:8787
    python dashboard.py --port 9000

Page polls /api/status every 4s. No build step, no framework.
"""
from __future__ import annotations

import base64
import concurrent.futures
import hmac
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from config import CONFIG  # triggers load_dotenv() — must precede `state` import

ROOT = Path(__file__).resolve().parent
LOG_PATH = ROOT / CONFIG.log_file

_SCAN_RE = re.compile(
    r"\[(?P<t>[\d:]+)\]\s+scan:\s+(?P<symbols>\d+)\s+\w+"  # [HH:MM:SS] scan: N universe/symbols
    r"(?:\s*\|[^|]*)?"                                      # optional extra segment (prescreen=N scored=N)
    r"\s*\|\s*open=(?P<open>\d+)"
    r"\s*\|\s*realized=\$(?P<realized>-?[\d.]+)"
    r"\s*\|\s*dayPnL=\$(?P<day>-?[\d.]+)"
    r"\s*\|\s*cb=(?P<cb>\w+)"
    r"\s*\|\s*regime=(?P<regime>\S+)"
)
_START_RE = re.compile(r"\[(?P<t>[\d:]+)\].*JARVIS engine starting.*llm=(?P<llm>on|off)")
# [HH:MM:SS] OPEN  EQUITY | paper | conf 0.72 (high) | BUY AAPL @ 150.25 x10 $1502.50 | thesis | agents A:0.6 ...
_OPEN_RE = re.compile(
    r"\[(?P<t>[\d:]+)\]\s+OPEN\s+(?P<asset>\w+)\s+\|\s+(?P<mode>\w+)\s+\|\s+"
    r"conf\s+(?P<conf>[\d.]+)\s+\((?P<clabel>\w+)\)\s+\|\s+"
    r"(?P<side>BUY|SELL)\s+(?P<sym>\w+)\s+@\s+(?P<price>[\d.]+)\s+x(?P<qty>\d+)\s+"
    r"\$(?P<size>[\d.]+)\s+\|\s+(?P<thesis>[^|]+?)\s+\|\s+agents\s+(?P<agents>.*)$"
)
# [HH:MM:SS] CLOSE stop-loss AAPL @ 148.10 pnl=$-21.50
_EXIT_RE = re.compile(
    r"\[(?P<t>[\d:]+)\]\s+(?P<act>CLOSE|EXIT-SHADOW)\s+(?P<reason>[\w-]+)\s+"
    r"(?P<sym>\w+)\s+@\s+(?P<price>[\d.]+)\s+pnl=\$(?P<pnl>-?[\d.]+)"
)


def _tail(path: Path, n: int) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(errors="replace").splitlines()[-n:]


def _parse_log() -> dict:
    lines = _tail(LOG_PATH, 1200)
    latest = None
    runs = 0
    series = []
    decisions = []
    for ln in lines:
        if _START_RE.search(ln):
            runs += 1
        m = _SCAN_RE.search(ln)
        if m:
            latest = {
                "time": m["t"], "symbols": int(m["symbols"]), "open": int(m["open"]),
                "realized": float(m["realized"]), "dayPnL": float(m["day"]),
                "cb": m["cb"], "regime": m["regime"],
            }
            series.append({"t": m["t"], "dayPnL": float(m["day"]), "realized": float(m["realized"])})
        o = _OPEN_RE.search(ln)
        if o:
            decisions.append({
                "time": o["t"], "act": "OPEN", "asset": o["asset"], "mode": o["mode"],
                "conf": float(o["conf"]), "clabel": o["clabel"], "side": o["side"],
                "sym": o["sym"], "price": float(o["price"]), "qty": int(o["qty"]),
                "size": float(o["size"]), "thesis": (o["thesis"] or "").strip(),
                "agents": (o["agents"] or "").strip(),
            })
            continue
        e = _EXIT_RE.search(ln)
        if e:
            decisions.append({
                "time": e["t"], "act": e["act"], "reason": e["reason"], "sym": e["sym"],
                "price": float(e["price"]), "pnl": float(e["pnl"]),
            })
    return {
        "latest": latest, "runs": runs, "series": series[-120:],
        "decisions": decisions[-30:][::-1], "feed": lines[-60:][::-1],
    }


def _fetch_positions_from_db() -> dict:
    """Inner DB fetch — runs in a worker thread so we can enforce a wall-clock timeout."""
    from state import State
    from marketdata import MarketData

    s = State.load()
    open_pos = s.open_positions

    symbols = list({p.symbol for p in open_pos})
    current_prices: dict[str, float] = {}
    if symbols:
        try:
            _md = MarketData()
            quotes = _md.quotes(symbols)
            current_prices = {sym: q.price for sym, q in quotes.items()}
            _md.close()
        except Exception:  # noqa: BLE001
            pass  # degrade gracefully — prices just show as "—"

    total_unrealized = 0.0
    open_data = []
    for p in open_pos:
        curr = current_prices.get(p.symbol)
        if curr is not None:
            direction = 1.0 if p.side == "BUY" else -1.0
            unrlzd = (curr - p.entry_price) * p.qty * direction
            total_unrealized += unrlzd
        else:
            unrlzd = None
        open_data.append({
            "symbol": p.symbol, "asset": p.asset, "side": p.side, "qty": p.qty,
            "entry": p.entry_price, "size_usd": p.size_usd,
            "stop": p.stop, "target": p.target, "thesis": p.thesis,
            "current_price": curr,
            "unrealized_pnl": unrlzd,
        })

    return {
        "source": "postgres",
        "realized_pnl": s.realized_pnl_usd, "shadow_pnl": s.shadow_pnl_usd,
        "day_pnl": s.daily_pnl(),
        "unrealized_pnl": total_unrealized if open_pos else None,
        "open": open_data,
        "closed": sum(1 for p in s.positions if not p.open and not p.shadow),
    }


# Shared thread pool so we don't spawn a new thread on every 4-second tick.
_DB_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="jarvis-db")
_DB_TIMEOUT_S = float(os.getenv("DASHBOARD_DB_TIMEOUT_S", "5"))


def _positions() -> dict:
    _stateless = {"source": "stateless", "realized_pnl": None, "shadow_pnl": None,
                  "day_pnl": None, "unrealized_pnl": None, "open": [], "closed": 0}
    if not os.getenv("DATABASE_URL"):
        return _stateless
    try:
        future = _DB_EXECUTOR.submit(_fetch_positions_from_db)
        return future.result(timeout=_DB_TIMEOUT_S)
    except concurrent.futures.TimeoutError:
        return {"source": "postgres-timeout", "realized_pnl": None,
                "shadow_pnl": None, "unrealized_pnl": None, "open": [], "closed": 0}
    except Exception as e:  # noqa: BLE001
        return {"source": f"postgres-error: {e}", "realized_pnl": None,
                "shadow_pnl": None, "unrealized_pnl": None, "open": [], "closed": 0}


def _config() -> dict:
    c = CONFIG
    backend = getattr(c, "llm_backend", "anthropic")
    eff_model = c.ollama_model if backend == "ollama" else c.llm_model
    return {
        "mode": c.trading_mode, "llm_enabled": c.llm_ready, "llm_model": eff_model,
        "llm_backend": backend, "broker": c.broker, "options_source": c.options_source,
        "interval_sec": c.scan_interval_sec, "bankroll_usd": c.bankroll_usd,
        "conf_threshold": c.confidence_threshold, "max_concurrent": c.max_concurrent,
        "max_position_pct": c.max_position_pct, "cramer_mode": c.cramer_mode,
        "trade_options": c.trade_options, "is_live": c.is_live,
        "db": bool(os.getenv("DATABASE_URL")),
    }


def status() -> dict:
    return {
        "now": datetime.now().astimezone().strftime("%H:%M:%S") + " ET",
        "config": _config(), "log": _parse_log(), "positions": _positions(),
    }


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>JARVIS · Trading</title>
<style>
  :root{
    --bg:#0a0e14; --panel:#0f1620; --panel2:#121b27; --line:#1d2a3a;
    --ink:#dfe9f2; --dim:#7d90a6; --faint:#54657a;
    --cyan:#36d4e6; --cyan-d:#1aa6b8; --green:#39d98a; --red:#ff6b6b; --amber:#ffcf5c;
    --glow:0 0 0 1px rgba(54,212,230,.18), 0 0 24px -8px rgba(54,212,230,.35);
  }
  *{box-sizing:border-box;}
  html,body{margin:0;height:100%;}
  body{background:
      radial-gradient(1200px 600px at 78% -10%, rgba(54,212,230,.08), transparent 60%),
      radial-gradient(900px 500px at -5% 110%, rgba(57,217,138,.06), transparent 55%),
      var(--bg);
    color:var(--ink);
    font:14px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;
    -webkit-font-smoothing:antialiased;}
  .wrap{max-width:1080px;margin:0 auto;padding:22px 20px 80px;}
  a{color:var(--cyan);text-decoration:none;}
  header{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap;
    padding-bottom:16px;border-bottom:1px solid var(--line);}
  .brand{display:flex;align-items:center;gap:12px;}
  .logo{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;
    background:linear-gradient(135deg,var(--cyan),var(--cyan-d));color:#04222a;font-weight:800;
    box-shadow:var(--glow);}
  .brand h1{font-size:16px;margin:0;letter-spacing:.14em;font-weight:700;}
  .brand .tag{font-size:11px;color:var(--dim);letter-spacing:.18em;text-transform:uppercase;}
  .hdr-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end;}
  .pill{display:inline-flex;align-items:center;gap:6px;padding:4px 11px;border-radius:999px;
    font-size:11px;font-weight:700;letter-spacing:.06em;border:1px solid var(--line);
    background:var(--panel);text-transform:uppercase;}
  .pill.paper{color:var(--green);border-color:rgba(57,217,138,.35);}
  .pill.live{color:var(--red);border-color:rgba(255,107,107,.4);}
  .pill.on{color:var(--cyan);border-color:rgba(54,212,230,.4);}
  .pill.off{color:var(--faint);}
  .pill.db{color:var(--amber);border-color:rgba(255,207,92,.35);}
  .pill.nodb{color:var(--faint);}
  .pill.cb-green{color:var(--green);border-color:rgba(57,217,138,.35);}
  .pill.cb-yellow{color:var(--amber);border-color:rgba(255,207,92,.45);}
  .pill.cb-red{color:var(--red);border-color:rgba(255,107,107,.5);}
  .heart{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--dim);}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--faint);}
  .dot.alive{background:var(--green);box-shadow:0 0 0 0 rgba(57,217,138,.7);animation:pp 1.8s infinite;}
  @keyframes pp{0%{box-shadow:0 0 0 0 rgba(57,217,138,.5);}70%{box-shadow:0 0 0 7px rgba(57,217,138,0);}100%{box-shadow:0 0 0 0 rgba(57,217,138,0);}}
  .grid{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:18px 0;}
  @media(max-width:900px){.grid{grid-template-columns:repeat(3,1fr);}}
  @media(max-width:560px){.grid{grid-template-columns:repeat(2,1fr);}}
  .card{position:relative;background:linear-gradient(180deg,var(--panel2),var(--panel));
    border:1px solid var(--line);border-radius:14px;padding:14px 16px;overflow:hidden;}
  .card::before{content:"";position:absolute;inset:0 auto 0 0;width:2px;
    background:linear-gradient(var(--cyan),transparent);opacity:.5;}
  .card .k{font-size:10.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--dim);}
  .card .v{font-size:27px;font-weight:700;margin-top:6px;letter-spacing:-.02em;
    font-variant-numeric:tabular-nums;}
  .card .s{font-size:11px;color:var(--faint);margin-top:2px;}
  .pos{color:var(--green);} .neg{color:var(--red);} .neutral{color:var(--ink);}
  .cols{display:grid;grid-template-columns:1.4fr 1fr;gap:14px;margin-top:6px;}
  @media(max-width:880px){.cols{grid-template-columns:1fr;}}
  .panel{background:linear-gradient(180deg,var(--panel2),var(--panel));
    border:1px solid var(--line);border-radius:14px;padding:16px;}
  .panel h2{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--dim);
    margin:0 0 12px;font-weight:700;display:flex;align-items:center;gap:8px;}
  .panel h2::before{content:"";width:6px;height:6px;border-radius:2px;background:var(--cyan);
    box-shadow:0 0 8px var(--cyan);}
  .spark{width:100%;height:84px;display:block;}
  .sparkmeta{display:flex;justify-content:space-between;font-size:11px;color:var(--faint);margin-top:6px;}
  .dec{border-left:2px solid var(--line);padding:9px 0 9px 12px;margin-left:2px;}
  .dec+.dec{border-top:1px solid rgba(29,42,58,.5);}
  .dec.OPEN{border-left-color:var(--green);} .dec.CLOSE{border-left-color:var(--amber);}
  .dec.EXIT-SHADOW{border-left-color:var(--faint);}
  .dec .top{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;}
  .dec .act{font-weight:700;letter-spacing:.05em;font-size:11px;}
  .dec.OPEN .act{color:var(--green);} .dec.CLOSE .act{color:var(--amber);}
  .dec .t{color:var(--faint);font-variant-numeric:tabular-nums;}
  .dec .q{font-size:13px;margin-top:4px;color:var(--ink);}
  .dec .meta{font-size:11px;color:var(--dim);margin-top:3px;}
  .chip{font-size:10px;padding:1px 7px;border-radius:6px;border:1px solid var(--line);color:var(--dim);}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th,td{text-align:left;padding:8px 8px;border-bottom:1px solid rgba(29,42,58,.6);}
  th{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);font-weight:700;}
  td.num{font-variant-numeric:tabular-nums;text-align:right;}
  .empty{color:var(--faint);padding:18px 0;text-align:center;font-size:13px;}
  pre{margin:0;background:#070b11;border:1px solid var(--line);border-radius:12px;
    padding:13px 14px;overflow:auto;max-height:300px;color:#9fb3c8;
    font:12px/1.65 ui-monospace,SFMono-Regular,Menlo,monospace;}
  .cfg{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;}
  .cfg .item{font-size:11px;color:var(--dim);background:var(--panel);border:1px solid var(--line);
    border-radius:8px;padding:5px 9px;}
  .cfg .item b{color:var(--ink);font-weight:600;}
  .full{grid-column:1/-1;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">J</div>
      <div><h1>JARVIS</h1><div class="tag">Trading Command</div></div>
    </div>
    <div class="hdr-right">
      <span id="pills"></span>
      <span class="heart"><span class="dot" id="dot"></span><span id="hb">linking…</span></span>
    </div>
  </header>

  <div class="grid" id="stats"></div>

  <div class="cols">
    <div class="panel">
      <h2>Day P&amp;L</h2>
      <svg class="spark" id="spark" viewBox="0 0 600 84" preserveAspectRatio="none"></svg>
      <div class="sparkmeta"><span id="spk-lo"></span><span id="spk-hi"></span></div>
    </div>
    <div class="panel">
      <h2>Engine</h2>
      <div class="cfg" id="cfg"></div>
    </div>
    <div class="panel full">
      <h2>Open Positions</h2>
      <div id="positions"></div>
    </div>
    <div class="panel full">
      <h2>Agent Decisions</h2>
      <div id="decisions"></div>
    </div>
    <div class="panel full">
      <h2>Raw Feed</h2>
      <pre id="feed">loading…</pre>
    </div>
  </div>
</div>

<script>
const money=n=>n==null?"—":(n<0?"-$":"$")+Math.abs(n).toFixed(2);
const cls=n=>n>0?"pos":(n<0?"neg":"neutral");
const esc=s=>(s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

function spark(series){
  const el=document.getElementById("spark");
  if(!series.length){el.innerHTML="";return;}
  const ys=series.map(p=>p.dayPnL), n=ys.length;
  let lo=Math.min(0,...ys), hi=Math.max(0,...ys); if(hi===lo)hi=lo+1;
  const W=600,H=84,pad=6;
  const x=i=>n<2?W/2:pad+i*(W-2*pad)/(n-1);
  const y=v=>H-pad-(v-lo)*(H-2*pad)/(hi-lo);
  const d=ys.map((v,i)=>(i?"L":"M")+x(i).toFixed(1)+" "+y(v).toFixed(1)).join(" ");
  const area=d+` L ${x(n-1).toFixed(1)} ${H} L ${x(0).toFixed(1)} ${H} Z`;
  const zero=y(0).toFixed(1);
  const up=ys[ys.length-1]>=0;
  const col=up?"#39d98a":"#ff6b6b";
  el.innerHTML=
    `<defs><linearGradient id="g" x1="0" x2="0" y1="0" y2="1">
       <stop offset="0" stop-color="${col}" stop-opacity=".28"/>
       <stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
     <line x1="0" y1="${zero}" x2="600" y2="${zero}" stroke="#1d2a3a" stroke-dasharray="3 4"/>
     <path d="${area}" fill="url(#g)"/>
     <path d="${d}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round"/>`;
  document.getElementById("spk-lo").textContent=money(lo);
  document.getElementById("spk-hi").textContent=money(hi);
}

async function tick(){
  let s;
  try{ s=await (await fetch("/api/status",{cache:"no-store"})).json(); }
  catch(e){ document.getElementById("dot").className="dot";
            document.getElementById("hb").textContent="bot offline"; return; }
  const c=s.config,L=s.log,P=s.positions,latest=L.latest;
  const cb=latest?latest.cb:"green";

  document.getElementById("pills").innerHTML=
    `<span class="pill ${c.is_live?'live':'paper'}">${c.mode}</span>`+
    `<span class="pill ${c.llm_enabled?'on':'off'}">LLM ${c.llm_enabled?(c.llm_backend==='ollama'?'LOCAL':'ON'):'OFF'}</span>`+
    `<span class="pill on">${esc(c.broker)}</span>`+
    `<span class="pill cb-${cb}">CB ${cb}</span>`+
    `<span class="pill ${c.db?'db':'nodb'}">${c.db?'DB':'NO DB'}</span>`;

  const realized=(P.realized_pnl!=null)?P.realized_pnl:(latest?latest.realized:null);
  const day=(P.day_pnl!=null)?P.day_pnl:(latest?latest.dayPnL:null);
  const shadow=P.shadow_pnl;
  const unrealized=(P.unrealized_pnl!=null)?P.unrealized_pnl:null;
  document.getElementById("stats").innerHTML=[
    ["Symbols",latest?latest.symbols:"—","watchlist","neutral"],
    ["Open",(P.open?P.open.length:(latest?latest.open:0)),(P.closed?P.closed+" closed":"positions"),"neutral"],
    ["Realized P&L",money(realized),"all-time",cls(realized||0)],
    ["Unrealized P&L",money(unrealized),"open positions",cls(unrealized||0)],
    ["Day P&L",money(day),"since 00:00 UTC",cls(day||0)],
  ].map(([k,v,sub,kls])=>`<div class="card"><div class="k">${k}</div>
     <div class="v ${kls}">${v}</div><div class="s">${sub}</div></div>`).join("");

  spark(L.series||[]);

  const cramer=c.cramer_mode?`shadow ${money(shadow)}`:"off";
  document.getElementById("cfg").innerHTML=[
    ["model",c.llm_model],["interval",c.interval_sec+"s"],["bankroll",money(c.bankroll_usd)],
    ["conf≥",c.conf_threshold],["maxPos",(c.max_position_pct*100).toFixed(1)+"%"],
    ["maxConc",c.max_concurrent],["options",c.options_source],
    ["regime",latest?latest.regime:"—"],["cramer",cramer],["runs",L.runs],
  ].map(([k,v])=>`<span class="item">${k} <b>${esc(String(v))}</b></span>`).join("");

  const rows=P.open||[];
  document.getElementById("positions").innerHTML=rows.length?
    `<table><thead><tr><th>Symbol</th><th>Side</th><th class="num">Qty</th>
      <th class="num">Entry</th><th class="num">Curr Price</th>
      <th class="num">Unrlzd P&amp;L</th><th class="num">Size</th>
      <th class="num">Stop</th><th class="num">Target</th></tr></thead><tbody>`+
    rows.map(p=>`<tr><td>${esc(p.symbol)}</td><td>${esc(p.side)}</td>
      <td class="num">${p.qty}</td>
      <td class="num">${(p.entry??"").toFixed?p.entry.toFixed(2):p.entry}</td>
      <td class="num">${p.current_price!=null?p.current_price.toFixed(2):"—"}</td>
      <td class="num ${p.unrealized_pnl!=null?cls(p.unrealized_pnl):''}">${money(p.unrealized_pnl)}</td>
      <td class="num">${money(p.size_usd)}</td>
      <td class="num">${p.stop?p.stop.toFixed(2):"—"}</td>
      <td class="num">${p.target?p.target.toFixed(2):"—"}</td></tr>`).join("")+`</tbody></table>`
    :`<div class="empty">No open positions. ${c.db?'':'(no DB — open positions not persisted)'}</div>`;

  const ds=L.decisions||[];
  document.getElementById("decisions").innerHTML=ds.length?ds.map(d=>{
    if(d.act==="OPEN"){
      return `<div class="dec OPEN"><div class="top">
        <span class="act">OPEN</span><span class="t">${d.time}</span>
        <span class="chip">${esc(d.asset)}</span>
        <span class="chip">conf ${d.conf} · ${esc(d.clabel)}</span>
        <span class="chip">${d.side} ${esc(d.sym)} @ ${d.price} ×${d.qty}</span>
        <span class="chip">${money(d.size)}</span></div>
       <div class="q">${esc(d.thesis)}</div>
       <div class="meta">agents ${esc(d.agents)}</div></div>`;
    }
    return `<div class="dec ${d.act}"><div class="top">
       <span class="act">${d.act}</span><span class="t">${d.time}</span>
       <span class="chip">${esc(d.reason)}</span>
       <span class="chip">${esc(d.sym)} @ ${d.price}</span>
       <span class="chip ${cls(d.pnl)}">${money(d.pnl)}</span></div></div>`;
  }).join(""):`<div class="empty">No trades yet — engine is watching.</div>`;

  document.getElementById("feed").textContent=(L.feed||[]).join("\n")||"no activity yet";
  document.getElementById("dot").className="dot alive";
  document.getElementById("hb").textContent=
    `scan ${latest?latest.time:"—"} · ${s.now} · ${P.source}`;
}
tick(); setInterval(tick,4000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        # A browser that polls /api/status and navigates away closes the socket
        # mid-response; that surfaces as BrokenPipe/ConnectionReset. The client
        # is already gone, so there's nothing to recover — swallow it silently
        # instead of letting it bubble up as a stderr traceback on every poll.
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args) -> None:  # noqa: N802
        pass  # silence per-request access logging to stderr

    def _auth_ok(self) -> bool:
        """HTTP Basic Auth, enabled only when DASH_USER + DASH_PASS are both set.
        Constant-time compare. Unset = open (local/dev)."""
        user = os.environ.get("DASH_USER", "")
        pw = os.environ.get("DASH_PASS", "")
        if not (user and pw):
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(hdr[6:]).decode("utf-8", "replace").partition(":")
        except Exception:  # noqa: BLE001
            return False
        return hmac.compare_digest(u, user) and hmac.compare_digest(p, pw)

    def _deny(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="JARVIS"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        # Unauthenticated health endpoint — Railway healthcheck hits this, not "/".
        if self.path == "/healthz":
            self._send(200, b"ok", "text/plain")
            return
        if not self._auth_ok():
            self._deny()
            return
        if self.path.startswith("/api/status"):
            # Build the body BEFORE sending so a client disconnect during the
            # write (handled inside _send) isn't misread as a status() failure
            # and retried down the 500 path (which produced a second traceback).
            try:
                body = json.dumps(status()).encode()
            except Exception as e:  # noqa: BLE001
                self._send(500, json.dumps({"error": str(e)}).encode(), "application/json")
                return
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/restart"):
            ks_path = ROOT / os.environ.get("KILL_SWITCH_FILE", getattr(CONFIG, "kill_switch_file", "KILL_SWITCH"))
            if ks_path.exists():
                try:
                    ks_path.unlink()
                except OSError:
                    pass
            logging.info("restart requested via API")
            self._send(200, json.dumps({"status": "restarting"}).encode(), "application/json")

            def _do_restart() -> None:
                os.execv(sys.executable, [sys.executable] + sys.argv)

            threading.Timer(0.5, _do_restart).start()
        elif self.path.startswith("/api/stop"):
            ks_path = ROOT / os.environ.get("KILL_SWITCH_FILE", getattr(CONFIG, "kill_switch_file", "KILL_SWITCH"))
            try:
                ks_path.touch()
            except OSError:
                pass
            logging.info("stop requested via API")
            self._send(200, json.dumps({"status": "stopping"}).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:
        pass


def start_background(port: int | None = None) -> None:
    """Start the dashboard in a background daemon thread (called by run.py)."""
    import threading

    p = port or int(os.environ.get("PORT", 8787))
    srv = ThreadingHTTPServer(("0.0.0.0", p), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True, name="jarvis-dashboard")
    t.start()
    print(f"JARVIS dashboard started → http://0.0.0.0:{p}")


def main() -> int:
    port = int(os.environ.get("PORT", 8787))
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"JARVIS dashboard → http://0.0.0.0:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
