# JARVIS — agentic stock + options trading engine

A multi-agent trading bot (2026 framework). Reads market structure + dealer
positioning + news, runs a Claude agent team, and trades equities through
Alpaca (paper by default). Built to grow an edge iteratively — feed it data,
tune the streams, measure.

> Not financial advice. Trading risks substantial loss. Run **paper** for weeks
> before risking a cent. ~92% of retail traders lose money.

## The pipeline (per cycle)

```
data ─┬─ quant stream  (SMA cross + RSI + ATR)            ┐
      └─ qual stream   (Analyst → bull/bear debate)       ├─ CONFLUENCE
                                                          ┘   (must agree)
   → Portfolio (conviction-weighted sizing)
   → Risk Manager (gamma-wall / regime veto)
   → Risk gate (kill switch, circuit breaker, drawdown, cooldown, sizing)
   → Execution (Alpaca / IBKR-stub / Sim)
   → manage open positions (ATR bracket exits)
```

A trade fires **only when both streams agree on direction** and the blended
confidence clears `CONFIDENCE_THRESHOLD`.

## Multi-agent team (`agents.py`)
- **Analyst** — fuses technicals, dealer positioning, macro, news → directional lean.
- **Researchers** — bull vs bear debate that adjusts the analyst's conviction.
- **Portfolio** — allocates conviction-weighted weights across candidates.
- **Risk Manager** — structural veto: no longs into a call wall, no longs in a
  negative-gamma regime below the gamma flip, etc.
- **Execution** — `executor.py` → broker.

With no `ANTHROPIC_API_KEY` the team degrades to neutral and the bot trades the
**quant stream alone** (paper) so the loop runs while you wire keys.

## Options analytics — the exposure stack (`options.py`)
GEX / DEX / VEX / CHEX per strike → **gamma flip**, **call/put walls**, **vol
trigger**. The level math is implemented; the data adapters
(`flashalpha`, `chain`) are stubs — set `OPTIONS_SOURCE` and supply a feed to
light it up. Until then `OPTIONS_SOURCE=none` and equities trade fine.

## Safety layers (`risk.py`)
- **Kill switch** — `touch KILL_SWITCH` halts new entries instantly.
- **Circuit breakers** — regime move >5% halves size (yellow), >10% halts (red).
  The loop also polls faster (`FAST_INTERVAL_SEC`) when tripped.
- **Daily drawdown** — pause once down `DAILY_DRAWDOWN_PCT` for the day.
- **Falling-knife cooldown** — per-symbol re-entry lockout.
- **ATR brackets** — stops/targets that travel with volatility.
- **Cramer mode** — runs an inverse shadow book; if it beats the real book your
  signals are systematically flawed. Shadow P&L shows on the dashboard.

## Setup
```bash
cd polymarket-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit: ALPACA_API_KEY/SECRET (paper) + ANTHROPIC_API_KEY
```
Get free Alpaca **paper** keys at app.alpaca.markets. No keys yet? Set
`BROKER=sim` and `LLM_ENABLED=false` to dry-run the loop offline.

## Run
```bash
python run.py --once        # single cycle (cron-friendly)
python run.py               # continuous loop, cadence adapts to volatility
```
JARVIS dashboard → http://127.0.0.1:8787

## Tests
```bash
pip install -r requirements-dev.txt
python -m pytest tests/      # deterministic, no network / no API key
```

## Files
| file | role |
|------|------|
| `config.py` | env → typed config + validation |
| `marketdata.py` | Alpaca bars/quotes + intraday-change feed |
| `signals.py` | `Signal` + technical indicators + quant stream |
| `options.py` | GEX/DEX/VEX/CHEX exposure stack + gamma flip / walls |
| `agents.py` | Analyst → debate → Risk-Manager team + Portfolio allocator |
| `risk.py` | kill switch, circuit breakers, drawdown, cooldown, sizing, ATR exits |
| `broker.py` | Alpaca / IBKR-stub / Sim execution adapters |
| `executor.py` | Signal → broker order → Position (+ Cramer shadow) |
| `state.py` | positions + P&L (real + shadow), Postgres-backed |
| `notifier.py` | console / file / Discord |
| `engine.py` | the agentic cycle |
| `run.py` | entrypoint / adaptive loop |
| `dashboard.py` | JARVIS web command center |

## Going live (real money)
Only after paper P&L convinces you. In `.env`: `TRADING_MODE=live` + live Alpaca
keys. Risk limits in `.env` are enforced in `risk.py` on every trade.

## Edge backlog (feed me data to build these)
- Real news/sentiment into `SymbolContext.news` (analyst is starved without it).
- Options feed for `OPTIONS_SOURCE` → live GEX walls drive the Risk Manager.
- Random-Forest quant model replacing the indicator stub in `signals.py`.
- Per-strategy backtest harness to measure edge before live.
