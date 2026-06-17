# JARVIS Trading Bot — Stack & Process (for NotebookLM research direction)

## What it is
An autonomous, options-primary intraday trading bot. Trades **0DTE options on index ETFs**
(SPY/QQQ/IWM) driven by **options dealer-gamma positioning (GEX)**, with an equity fallback
on a 40-name universe. Paper trading on Alpaca. Runs locally (Python) on a Mac.

## Tech stack
- **Language/runtime:** Python 3.9, local process, Postgres (Railway) for state, HUD dashboard on :8787
- **Broker / execution:** Alpaca (paper; options approval **Level 3** = long + spreads)
- **Options data / GEX:** CBOE free delayed quotes (per-contract OI + greeks + IV + bid/ask) — no key
- **Underlying price/bars:** Alpaca IEX (5-Min intraday bars + latest-trade quote)
- **Macro regime:** FRED (VIX, 10y, fed funds)
- **News + event risk:** Finnhub (company news, earnings calendar, economic calendar)
- **Enrichment:** yfinance (forward earnings guard, IV rank, analyst consensus, headlines)
- **LLM "agent team":** local Ollama `qwen2.5:7b` (free), 5 roles, temperature 0, strict JSON

## Strategy — Dealer Gamma Regime Breakout
- **Negative gamma** (price trending/"slippery"): **long ATM 0DTE** call/put (debit) — delta outpaces theta
- **Positive gamma** (price sticky/mean-reverting): **credit spread** (bull-put / bear-call), short leg at the wall
- **Levels:** gamma **flip** = regime line / stop; **call/put walls** = resistance/support + spread strikes; **0DTE magnet** = profit target
- **Exits:** underlying hits magnet (target) or reclaims flip (stop); hard **flat by 3:30pm ET** (charm unwind)

## Per-cycle process (~every 60s, ~3.5min with LLM)
1. Reconcile broker fills
2. Regime + circuit breaker (SPY intraday move: >5% halve size, >10% halt)
3. Manage open exits (level stops, 3:30pm option time-stop)
4. **2-stage funnel:** cheap quant prescreen on all 40 names → rank → top 20 get the expensive LLM+GEX pass
5. **Confluence:** quant indicators (SMA/RSI/ATR) AND LLM team must agree on direction, blended confidence ≥ threshold
6. Guards: yfinance earnings window + Finnhub event blackout (US high-impact macro / earnings)
7. **Options-primary:** on SPY/QQQ/IWM with a live GEX regime → build + price + liquidity-check a 0DTE structure; else equity
8. Portfolio sizing across top concurrent slots → risk gate → execute (**option signals first**)
9. Premium-based P&L marking

## Key parameters (current)
- Universe: 40 liquid/high-OI/high-beta optionable names; options gated to SPY/QQQ/IWM (daily 0DTE)
- Capital: $5,000 bankroll, 5% max/position, max 10 concurrent, min trade $20
- Confidence threshold 0.45; min confidence "medium"
- Options filters: bid/ask spread ≤20% (or ≤$0.10 abs), min mid $0.05, strike step from live chain
- Event blackout: US high-impact macro ±2h; earnings ±~1 day
- Intraday timeframe: 5-Min bars; LLM cap 20 symbols/cycle

## Known gaps (where research helps most)
- **Real-time data**: CBOE/IEX are ~15-min delayed (fine for paper, too slow for live 0DTE entries) — Tastytrade/dxFeed or paid OPRA would fix
- **Futures**: no ES/NQ yet (Alpaca can't; Tastytrade/IBKR could)
- **Edge measurement**: no options backtest (no free historical GEX) — forward-testing only
- **Order flow / microstructure** (OBI/footprint absorption) not wired — the strategy references it but it needs paid L2

## Good NotebookLM research directions
- Optimal 0DTE structure selection by gamma regime + DTE + IV rank
- GEX level reliability per underlying; when walls/flip hold vs fail
- Post-macro-print (CPI/FOMC) 0DTE behavior and entry timing
- Tastytrade/dxFeed real-time greeks integration for live trading
- Position sizing / Kelly for defined-risk option spreads on a small account
