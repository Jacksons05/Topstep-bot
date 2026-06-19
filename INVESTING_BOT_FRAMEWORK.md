# Investing Bot — Architecture Framework

> **Status:** Design / Pre-implementation  
> **Distinction from JARVIS:** JARVIS is a short-term active trading bot (intraday options, equities, 0DTE).  
> This bot is a **long-term capital allocator** — rebalances a portfolio on a weekly/monthly cadence,  
> holds positions for weeks to years, and optimizes for risk-adjusted compounding, not daily P&L.

---

## 1. Philosophy & Goals

| Dimension | JARVIS (active trader) | Investing Bot (this) |
|---|---|---|
| Hold period | Minutes → days | Weeks → years |
| Signal source | GEX, momentum, 0DTE flow | Fundamentals, macro, factor tilts |
| Rebalance cadence | Every scan cycle (30s) | Weekly / monthly / threshold |
| Objective | Daily P&L | Risk-adjusted long-term compounding |
| Risk frame | Max drawdown per day | Volatility, Sharpe, max drawdown over quarters |
| Leverage | None / minimal | None (fully-funded positions only) |
| Options | Core to strategy | Not used (unless covered calls overlay added later) |

---

## 2. Asset Universe

### Tier 1 — Core Holdings (80-90% of portfolio)
- **US broad market ETFs**: SPY, QQQ, IWM, VTI, VOO
- **International**: EFA (developed), EEM (emerging)
- **Fixed income**: TLT (long bonds), AGG (aggregate), BIL (T-bills/cash)
- **Alternatives**: GLD (gold), VNQ (REITs)

### Tier 2 — Factor Tilts (10-20% of portfolio)
- **Value**: VTV, AVLV
- **Small-cap value**: IJS, AVUV
- **Momentum**: MTUM
- **Quality / dividend**: SCHD, VIG
- **Sector overweights**: XLK, XLV, XLE (regime-dependent)

### Tier 3 — Individual Stock Sleeve (0-10%, optional)
- High-conviction names from fundamental screen
- Hard cap: 3% per name, max 5 names

---

## 3. Signal Stack

### 3a. Macro Regime Signal (top-down allocation)
Determines broad equity/bond/cash split.

```
Inputs:
  - Fed Funds rate (FRED: FEDFUNDS)
  - 10Y–2Y yield spread / inversion flag (FRED: T10Y2Y)
  - CPI YoY (FRED: CPIAUCSL)
  - ISM Manufacturing PMI
  - VIX regime (low < 15, normal 15-25, high > 25)
  - SPY 200-day SMA (above = risk-on, below = risk-off)

Output: REGIME = {risk_on, neutral, risk_off, crisis}

Mapping:
  risk_on  → 90% equities, 5% bonds, 5% gold
  neutral  → 70% equities, 20% bonds, 10% gold/cash
  risk_off → 40% equities, 40% bonds, 20% gold/cash
  crisis   → 20% equities, 40% bonds, 20% gold, 20% T-bills
```

### 3b. Factor Signal (equity tilt within the equity sleeve)
```
Momentum  : 12-1 month return (skip last month)
Value     : P/E, P/B, EV/EBITDA vs sector median
Quality   : ROE > 15%, debt/equity < 1, positive FCF
Low-vol   : 252-day realized vol relative to universe
```

### 3c. Individual Stock Screen (Tier 3 only)
```
Universe  : S&P 500 components
Filters   : Market cap > $10B, avg volume > 2M shares/day
Scores    : Composite of value (30%) + quality (30%) + momentum (20%) + analyst revision (20%)
Threshold : Top-decile score AND analyst upgrade in last 30 days
```

---

## 4. Portfolio Construction

### Target Weight Engine
```python
def compute_target_weights(regime, factor_scores, tier3_picks):
    # 1. Set macro envelope from regime
    equity_budget = REGIME_EQUITY_BUDGET[regime]
    bond_budget   = REGIME_BOND_BUDGET[regime]
    alt_budget    = REGIME_ALT_BUDGET[regime]

    # 2. Allocate equity budget across tiers
    tier1_equity  = equity_budget * 0.70   # core ETFs
    tier2_equity  = equity_budget * 0.20   # factor tilts
    tier3_equity  = equity_budget * 0.10   # individual stocks (if picks exist)

    # 3. Within each tier, weight by factor scores (or equal-weight as fallback)
    # 4. Apply constraints:
    #    - Single ETF max: 30%
    #    - Single stock max: 3%
    #    - Sector max: 35%
    #    - Min position: 1% (avoid rounding noise)

    return target_weights   # dict: symbol → fraction of portfolio
```

### Optimization Approach
- **Default**: Risk-parity (equal risk contribution) across tiers
- **Alternative**: Mean-variance with historical covariance (use polars + scipy)
- **Fallback**: Equal-weight within each tier (if <12 months of data)

---

## 5. Rebalancing Logic

### Triggers
1. **Calendar**: Monthly on first trading day (configurable: weekly / monthly / quarterly)
2. **Drift threshold**: Any position drifts >5% absolute from target → immediate rebalance
3. **Regime change**: Macro regime flips → rebalance within 1 trading day
4. **New signal**: Tier 3 stock enters/exits top decile → swap position

### Execution
```
1. Compute current weights from broker positions + prices
2. Compute target weights
3. Compute diff (target - current)
4. Filter out trades < $50 or < 0.5% drift (avoid churn)
5. Sort: sells first (free up cash), then buys
6. Submit market-at-open orders for each leg (via Alpaca)
7. Log rebalance event to Postgres with reason code
```

### Tax-Aware Mode (future)
- Prefer selling losers (tax-loss harvesting)
- Avoid wash-sale window (30-day lookback before re-buy of same position)
- Flag long-term vs short-term gain status

---

## 6. Risk Controls

### Position Limits
```
max_single_etf_pct    = 0.30   # no more than 30% in one ETF
max_single_stock_pct  = 0.03   # no more than 3% in any individual stock
max_sector_pct        = 0.35   # sector concentration cap
max_equity_pct        = 0.95   # always hold some bonds/cash unless crisis regime and configured otherwise
min_cash_pct          = 0.02   # always keep 2% liquid for rebalance slippage
```

### Drawdown Controls
```
portfolio_max_drawdown = 0.25   # from all-time high: halt new buys, reduce to neutral
equity_circuit_breaker = 0.15   # SPY/VTI falls >15% in 30 days → shift to risk_off
```

### Volatility Targeting (optional)
```
target_portfolio_vol = 0.12    # 12% annualized volatility target
If realized_vol > target: scale down equity exposure
If realized_vol < target: scale up (to max_equity_pct cap)
```

---

## 7. Data Layer

| Data | Source | Cadence | Notes |
|---|---|---|---|
| Prices (EOD) | yfinance / Alpaca data API | Daily | Already have yf_data.py |
| Fundamentals | yfinance info + EDGAR | Weekly | P/E, P/B, EPS, revenue |
| Macro indicators | FRED API | Weekly/monthly | Already have macro.py + FRED key |
| Analyst ratings | Finnhub | Weekly | Already have finnhub key |
| VIX | yfinance (^VIX) | Daily | Already pulled via regime.py |
| Economic calendar | Finnhub | Daily | Already have events.py |

**Schema additions (Postgres)**:
```sql
-- Portfolio target state
CREATE TABLE portfolio_targets (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMPTZ DEFAULT NOW(),
    regime VARCHAR(20),
    symbol VARCHAR(20),
    target_weight FLOAT,
    current_weight FLOAT,
    diff FLOAT
);

-- Rebalance log
CREATE TABLE rebalance_events (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason VARCHAR(50),   -- 'calendar' | 'drift' | 'regime_change' | 'signal'
    trades JSONB,                 -- [{symbol, side, qty, price}]
    pre_weights JSONB,
    post_weights JSONB
);

-- Portfolio performance
CREATE TABLE portfolio_performance (
    date DATE PRIMARY KEY,
    total_value FLOAT,
    daily_return FLOAT,
    benchmark_return FLOAT,   -- SPY
    sharpe_rolling_90d FLOAT,
    max_drawdown_90d FLOAT
);
```

---

## 8. Module Map

```
investing_bot/
├── run_investing.py          # entrypoint, daily scheduling loop
├── config_investing.py       # separate config (bankroll, risk params, universe)
├── macro_regime.py           # FRED pulls → regime classification
├── factor_scores.py          # momentum / value / quality scoring
├── stock_screen.py           # S&P 500 fundamental screen (Tier 3)
├── portfolio_weights.py      # target weight computation + optimization
├── rebalancer.py             # diff engine, trade list, execution via AlpacaBroker
├── risk_controls.py          # position limit checks, drawdown guard
├── performance.py            # Sharpe, max DD, benchmark comparison
├── db_investing.py           # Postgres read/write for portfolio tables
└── dashboard_investing.py    # lightweight HTTP status page (port 8788)
```

---

## 9. Execution Stack

- **Broker**: Alpaca (already integrated) — stocks + ETFs
- **Order type**: Market-at-open for rebalance, limit for individual stock entries
- **Scheduling**: `schedule` library or cron via `run_investing.py` — runs at 9:35 AM ET daily
- **Paper mode first**: `INVESTING_MODE=paper` mirrors full logic, no real orders
- **Live opt-in**: `INVESTING_MODE=live` — explicit opt-in per session (same pattern as JARVIS)

---

## 10. Key Open Questions

Before implementation starts, these design choices need your input:

1. **Account**: Separate Alpaca account from JARVIS? Or share the $5k paper account?
2. **Starting capital**: What's the target AUM for this bot? (Separate brokerage account?)
3. **Asset scope**: ETFs only (simpler), or include individual stock picks (Tier 3 sleeve)?
4. **Automation level**: Fully autonomous rebalances, or advisory mode (shows recommended trades, you approve before execution)?
5. **Tax awareness**: Is this in a taxable account (tax-loss harvesting matters) or IRA/tax-advantaged?
6. **Topstep integration**: Should this eventually tie into the Rithmic/Topstep account for futures overlay (e.g., add ES micro-futures as a hedge layer)?

---

## 11. Implementation Phases

### Phase 1 — Data & Signal (1-2 days)
- `macro_regime.py`: FRED pulls → regime enum
- `factor_scores.py`: momentum + value scoring on ETF universe
- Wire to existing `macro.py` and `yf_data.py`

### Phase 2 — Portfolio Engine (2-3 days)  
- `portfolio_weights.py`: target weight computation
- `risk_controls.py`: limit enforcement
- `db_investing.py`: schema creation + read/write

### Phase 3 — Rebalancer (1-2 days)
- `rebalancer.py`: diff → trade list → Alpaca execution
- Paper mode end-to-end test

### Phase 4 — Dashboard & Monitoring (1 day)
- `dashboard_investing.py`: port 8788, portfolio weights visualization
- Performance metrics: Sharpe, drawdown, benchmark vs SPY

### Phase 5 — Live & Tune
- Switch to live account after 2-4 weeks of paper validation
- Add tax-aware mode if taxable account
- Optional: covered-call overlay on core ETF holdings
