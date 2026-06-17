# JARVIS Intraday Strategy — Dealer Gamma Regime Breakout

Synthesized from all 53 sources in NotebookLM "Agentic AI Portfolio Management and Alpaca Trading Integration" (2026-06-09).

## 1. Edge
Exploits **mechanical, non-discretionary delta-hedging flows** of options dealers. Dealers buy/sell ES to stay delta-neutral as price/vol shift, creating "magnetic" support/resistance at high-OI strikes and accelerating momentum when gamma thresholds break.

## 2. Instruments
- **ES (E-mini S&P 500 futures)** — primary execution; high-fidelity tick data (250+ ticks/min), 24/5 liquidity.
- **SPX/SPY 0DTE options** — signal generation only; ~50-60% of daily volume, most acute intraday gamma pressure.

## 3. Signal Stack
- **Gamma regime** via Gamma Profile (recompute GEX across ±10% spot) → find **Zero Gamma / Gamma Flip** level.
  - **Positive gamma (> flip):** low vol, price sticky/mean-reverting.
  - **Negative gamma (< flip):** high vol, price slippery/trending.
- **Key levels:**
  - **Call Wall** — highest positive gamma strike, ceiling.
  - **Put Wall** — highest negative gamma strike, floor.
  - **Volatility Trigger** — last major positive-gamma support, sits a few pts above flip (early warning).
- **Order-flow confirmation:** Order Imbalance (OBI) near ±1.0 at level, OR footprint absorption (high volume, no price move) at a Wall.
- **Vol-edge:** IV/HV > 1.5 → favorable for premium selling in positive gamma.

## 4. Entry Rules — "Regime Breakout"
**Short:**
1. Price **below Volatility Trigger** and breaches **Gamma Flip**.
2. Price breaches **Put Wall** zone (strike ±0.5%).
3. Trigger: sharp negative-delta initiation OR resting buy-order pull (liquidity pull).

**Long:**
1. Price **above Volatility Trigger** in **positive gamma**.
2. Price approaches **High-Volume Node (HVN)** or **Put Wall** from above.
3. Trigger: buying absorption (negative delta at lows, no follow-through).

## 5. Exit Rules
- **Target:** 0DTE magnet strike or next major HVN.
- **Stop:** reclaim of Volatility Trigger OR 1.5× ATR from entry.
- **Time-stop:** flat all 0DTE by **3:30 PM ET** — avoid charm-decay hedge unwinds (30+ pt close reversals).
- **Invalidation:** intraday Net GEX flips sign.

## 6. Risk / Sizing
- Per-trade risk: **0.5% of NAV**.
- Vol-adjusted size: `MaxPos = (Portfolio × 0.10) / (ATR14 × 2.0)`.
- Daily max loss: kill-switch at **10% drawdown**.
- Max **2 concurrent positions**.

## 7. Agent Mapping (5-agent Claude + Alpaca/Tastytrade/IBKR)
1. **Options Analyst** — ingest SPX chain (Tastytrade API); compute GEX surface; ID flip, walls, trigger daily.
2. **Microstructure Agent** — ES L2/L3; OBI, micro-price, MAD-based Z-scores to spot institutional whales.
3. **Portfolio Agent** — vol-edge + Kelly sizing; enforce sector/NAV ceilings.
4. **Execution Agent** — centralized hub server (prevents IBKR/Alpaca session collisions); limit/bracket orders.
5. **Risk/Journal Agent** — real-time P&L; enforce 3:30 PM exit; log decisions for iteration.

## 8. Known Failure Modes
- **Positive-gamma pinning** — directional 0DTE dies in manufactured range, 100% theta bleed.
- **Stale data** — free news/sentiment APIs trade already-priced-in signal.
- **Liquidity gaps** — Triple Witching spreads widen ~15×, can't exit at model price.
- **Lookahead bias** — backtests must shift indicators T-1 (no end-of-day OI leakage) or fail live.

---
*Source caveat: 3 of 53 sources errored on ingest (paywalled): jats.substack SPX/ES levels, medium 900-hours-Claude-trading, signatureflowtrading ES levels. Re-add via login-cookie path if needed.*
