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

## 7. How it wires into this bot (WIRED)

- `orderflow.py` — pure-stdlib `OrderFlowEngine` implementing §2–§4 (OBI,
  micro-price, CVD + divergence, MAD-z whale, `confirm_entry()`).
- `rithmic_marketdata.py` — `RithmicOrderFlowFeed` reuses the **already-connected
  RithmicBroker client + background loop** (one socket, the "hub" pattern) and
  subscribes `DataType.BBO` + `DataType.LAST_TRADE` for each futures root in the
  watchlist via `client.subscribe_to_market_data()`. The `on_tick` handler routes
  BBO → `on_depth()` and trades → `on_trade()` per symbol.
- `engine.py` — builds the feed in `__init__` when LUCID/Rithmic is live and
  `ORDERFLOW_GATE_ENABLED`; applies `confirm_entry(sig.side)` as the final entry
  gate (after risk.check, before `executor.open`), and resets CVD each new day.
  **Fails open** when the engine has no data yet (`has_data` False) — warm-up,
  non-futures symbols, or mock mode never block trading.

Knobs: `ORDERFLOW_GATE_ENABLED` (master, default on) · `OF_OBI_THRESHOLD=0.85` ·
`OF_WHALE_Z=3.5` · `OF_WHALE_NOTIONAL_USD=1e6` · `OF_FOOTPRINT_RATIO=10` ·
`OF_WINDOW_SEC=120`.

## 8. Level 2 / full depth (CME DOM)

A funded Lucid futures account **is entitled to CME Level 2** — futures depth is
a single consolidated exchange book (DOM), cheap and standard, unlike fragmented
equity L2. Verified in the protocol: Rithmic's market-data `UpdateBits` defines
**`ORDER_BOOK = 4`** (full ladder) alongside `BBO=2` / `LAST_TRADE=1`.

The constraint is the **library, not the account**: `async-rithmic` exposes only
`DataType.BBO` + `LAST_TRADE`, ships the depth references **commented out**
(`base.py` `#156: pb.order_book_pb2.OrderBook`), and does **not** bundle
`order_book_pb2`. So it can *request* depth but can't decode the inbound message.

**What's built (ready, drop-in):**
- `orderflow.py` `MultiLevelBook` — price→size ladder per side; `depth_obi(N)`
  sums the top N levels. `OrderFlowEngine.obi` prefers depth OBI when the ladder
  is live, BBO otherwise. `on_depth_snapshot()` / `on_depth_update()` ingest.
- `rithmic_marketdata.py` — subscribes `ORDER_BOOK` (via the `_OrderBookBits`
  shim, `value=4`) when depth is available; `_register_depth_template()` wires
  template 156 into the plant decode map; the tick handler routes the depth
  arrays into the book. `OF_DEPTH_LEVELS` (default 5) sets the OBI depth.

**To finalize (one artifact + one confirm):**
1. Add Rithmic R|Protocol **`order_book.proto`** (template 156), compile to
   `order_book_pb2`, drop into `async_rithmic/protocol_buffers/` — then
   `_register_depth_template()` returns True and `depth_active` flips on.
2. Patch the ticker plant to forward template 156 to `on_tick` (it currently
   handles only 150/151), and **confirm the OrderBook field names** against a
   live tick (`bid_price[]`/`bid_size[]`/`ask_price[]`/`ask_size[]` assumed in
   `_depth_levels_from_msg`). The consumer side needs zero further change.

Until then the feed runs **top-of-book BBO + trades** (OBI/micro-price/CVD/whale
all work); only multi-level depth OBI / footprint is idle. The O(log N) tree
book / Fenwick VAP remain future optimizations on top of the ladder model.
