# Order-Flow Confirmation — Research Spec

Synthesized from the NotebookLM notebook *"Agentic AI Portfolio Management and
Alpaca Trading Integration"* (id `5af0f2cf-65d2-4241-9be4-5b13cb00f811`,
2026-06-16). The dealer-gamma / GEX breakout strategy treats order-flow as a
**required entry trigger** at walls and the flip — not optional. This bot
(Lucid/ES via Rithmic) is the natural home: the same Rithmic connection that
executes also streams the L2 depth + trade prints these metrics need.

## 1. Metrics + thresholds (as the sources state them)

| Metric | Threshold | Use |
|---|---|---|
| **OBI** (order-book imbalance) | approach **±1.0** at a gamma wall | extreme pressure → entry confirm |
| **MAD modified z-score** (whale-hunt) | **> 3.5** | flag institutional block trade |
| **Whale notional** | **≥ $1,000,000** on one contract in one second | institutional vs retail |
| **Vol-edge** IV/HV | **> 1.5** + positive GEX | premium-selling entry at call wall |
| **Footprint imbalance** | aggressive one side **≥ ~10:1** | absorption at wall |
| **Pin-risk zone** | within **0.5%** of major-OI strike, **< 2h** to expiry | pinning most acute |
| **CVD divergence** | price new extreme NOT confirmed by CVD | reversal / exhaustion |

## 2. Formulas (real-time from L2/L3)

- **OBI** = (L_b − L_a) / (L_b + L_a) — resting volume at best bid (L_b) / best ask (L_a). Range −1..+1.
- **Micro-price** = (p_ask·L_b + p_bid·L_a) / (L_b + L_a) — liquidity-weighted mid; leads the tape (note the *opposite-side* weighting).
- **MAD modified z** = 0.6745·(xᵢ − median(x)) / MAD, where MAD = median(|xᵢ − median(x)|). Robust to outliers; flag when > 3.5. Used on small samples (< 30 intervals).
- **Delta** (per bar) = AskVolume − BidVolume. **CVD** = running sum of delta since session open.

## 3. CVD construction + reversal rules

**Aggressor classification** (compare execution price to NBBO):
- Trade **at ask** → buyer-initiated → **+size** (positive delta)
- Trade **at bid** → seller-initiated → **−size** (negative delta)

**Reversal divergence:**
- **Bearish:** price sets a new high while CVD is declining → buying exhaustion.
- **Bullish:** price sets a new low while negative CVD moves back toward zero → seller exhaustion.

**Absorption:** high positive delta (aggressive buying) at a **Call Wall** but price fails to advance → passive absorption → the wall is holding.

## 4. Final entry confirmation at a gamma wall

A flagged setup combines, in order:
1. Price at a wall / flip zone (structural, from GEX).
2. **OBI → ±1.0** in the trade direction (extreme pressure).
3. **Whale flag** (MAD z > 3.5 and ≥ $1M/contract/second) on the same side, OR
4. **CVD divergence** (reversal) / **absorption** (continuation hold).
5. **HVN alignment** — wall coincides with a High-Volume Node → institutional acceptance.

## 5. Data feeds named by the sources

- **ES futures microstructure:** Rithmic API (≥143k events/session, 250+ ticks/min) · IBKR TWS via `ib_async` (L2 + exec) · **Bookmap** (stacked bids/offers, iceberg viz).
- **SPX/SPY 0DTE:** Massive.com (ex-Polygon) per-second transaction bars for the whale algo · **Tastytrade DXLink** (sub-second greeks + chain) · FlashAlpha (`pin_score` 0–100 + flip/walls).

## 6. Implementation notes (production architecture from sources)

- **Centralized hub server** over a single persistent **TCP socket** to Rithmic; multiplex multiple bots through one connection to avoid session timeouts / data gaps.
- **Order book:** Binary Tree + Doubly-Linked List → O(log M) price-level lookup, O(1) insert/delete/sweep.
- **Volume-at-Price (HVN):** Fenwick / Segment tree over price nodes.
- **Startup snapshot:** take one high-fidelity options-chain snapshot at open into in-memory Polars/LanceDB rather than polling all session.

## 7. How it wires into this bot

`orderflow.py` (this repo) implements §2–§4 in pure Python (stdlib only) so it's
testable offline with synthetic depth/trades. A live adapter feeds it from the
Rithmic L2 stream (`async-rithmic`, installed). `OrderFlowEngine.confirm_entry()`
returns a gate the engine can apply alongside the GEX level + the Risk-Manager
agent veto. The O(log N) tree book / Fenwick VAP are future optimizations — the
first cut keeps only best-bid/ask + a rolling trade window, which is all OBI,
micro-price, CVD, and the whale z-score require.
