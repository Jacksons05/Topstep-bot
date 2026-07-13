"""Typed config loaded from environment / .env.

Topstep futures trading bot (2026 agentic framework). Futures execute via the
ProjectX (TopstepX) REST + SignalR gateway; the Topstep risk layer enforces
trailing MLL, DLL, and contract caps on every order.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _f(key: str, default: float) -> float:
    return float(os.getenv(key, default))


def _i(key: str, default: int) -> int:
    return int(os.getenv(key, default))


def _b(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _s(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _csv(key: str, default: str = "") -> tuple[str, ...]:
    """Comma-separated env value -> tuple of trimmed, upper-cased items."""
    raw = os.getenv(key, default)
    return tuple(x.strip().upper() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Config:
    # ── engine loop ───────────────────────────────────────
    scan_interval_sec: int = _i("SCAN_INTERVAL_SEC", 60)
    # Adaptive cadence: when realized vol spikes, poll faster (down to this).
    fast_interval_sec: int = _i("FAST_INTERVAL_SEC", 15)
    # Cost control: only run the (LLM-heavy) scan during US market hours; idle
    # cheaply otherwise. CLOSED_INTERVAL_SEC is the slow poll while the market is
    # shut so the loop just checks the clock instead of burning API credits 24/7.
    market_hours_only: bool = _b("MARKET_HOURS_ONLY", True)
    closed_interval_sec: int = _i("CLOSED_INTERVAL_SEC", 900)
    watchlist: tuple[str, ...] = _csv("WATCHLIST", "ES,NQ,MES,MNQ")
    # Universe index used for regime / circuit-breaker reads.
    # QQQ tracks Nasdaq-100 (same underlying as NQ/MNQ) and is available on Alpaca.
    regime_symbol: str = _s("REGIME_SYMBOL", "QQQ")

    # Session-phase confidence multipliers (applied to quant strength/lean in
    # _prescreen). Open/close momentum windows get full weight; midday + overnight
    # are discounted for lower predictability (Gao et al. JFE 2018). Overnight is
    # tunable: raise toward 1.0 to open night trading, lower to suppress it.
    phase_mult_open: float = _f("PHASE_MULT_OPEN", 1.00)
    phase_mult_close: float = _f("PHASE_MULT_CLOSE", 0.85)
    phase_mult_midday: float = _f("PHASE_MULT_MIDDAY", 0.70)
    phase_mult_overnight: float = _f("PHASE_MULT_OVERNIGHT", 0.75)
    # Overnight confluence blend: quant strength is thin overnight, so trust the
    # qualitative (LLM) stream more. qual_weight is the LLM share of the blended
    # confidence; RTH uses a fixed 50/50. Overnight also gets its own (looser)
    # confidence gate so RTH stays strict.
    qual_weight_overnight: float = _f("QUAL_WEIGHT_OVERNIGHT", 0.65)
    confidence_threshold_overnight: float = _f("CONFIDENCE_THRESHOLD_OVERNIGHT", 0.58)

    # ── asset classes enabled ─────────────────────────────
    trade_equities: bool = _b("TRADE_EQUITIES", False)  # futures-only fork; flip True to re-enable equity scanning
    trade_options: bool = _b("TRADE_OPTIONS", False)   # needs OPTIONS data source

    # ── confluence thresholds ─────────────────────────────
    # A BUY requires the qualitative (LLM) and quantitative (indicator/ML)
    # streams to AGREE, plus a combined confidence over this floor.
    confidence_threshold: float = _f("CONFIDENCE_THRESHOLD", 0.60)
    min_confidence: str = _s("MIN_CONFIDENCE", "medium")   # low|medium|high

    # ── news / sentiment ──────────────────────────────────
    news_enabled: bool = _b("NEWS_ENABLED", True)
    # Multi-source aggregation: NEWS_SOURCES is a comma list pulled in parallel and
    # merged (round-robin, deduped) into one headline set per symbol. Each source
    # degrades to empty if it lacks a key, so listing extra sources is free.
    # Valid: google | rss | polygon | finnhub | alpaca | sec
    # Back-compat: if NEWS_SOURCES is unset, fall back to the single NEWS_SOURCE.
    news_sources_raw: tuple[str, ...] = _csv("NEWS_SOURCES", "google,alpaca,finnhub,sec")
    news_source: str = _s("NEWS_SOURCE", "google")         # legacy single-source fallback
    polygon_api_key: str = _s("POLYGON_API_KEY")           # only for source=polygon
    # custom RSS feed for source=rss; {q} is replaced by the ticker (URL-encoded).
    news_rss_template: str = _s(
        "NEWS_RSS_TEMPLATE",
        "https://news.google.com/rss/search?q={q}+stock&hl=en-US&gl=US&ceid=US:en",
    )
    news_per_symbol: int = _i("NEWS_PER_SYMBOL", 6)        # per-source pull cap
    news_max_headlines: int = _i("NEWS_MAX_HEADLINES", 10)  # aggregate cap fed to analyst
    news_lookback_hours: int = _i("NEWS_LOOKBACK_HOURS", 48)
    finnhub_api_key: str = _s("FINNHUB_API_KEY")           # news+sentiment, earnings/econ calendar
    # SEC EDGAR (free, no key) — 8-K material-event filings as headlines. SEC's fair-access
    # policy REQUIRES a descriptive User-Agent with a contact email; set yours here via the
    # SEC_USER_AGENT environment variable.  The default is a placeholder — set it to
    # "your-app-name your@email.com" before enabling the sec news source in production.
    sec_user_agent: str = _s("SEC_USER_AGENT", "jarvis-stock contact@example.com")
    sec_form_types: tuple[str, ...] = _csv("SEC_FORM_TYPES", "8-K")  # filing types to surface
    # Filings are sparse and stay material far longer than a news headline, so they
    # use their own (much longer) lookback rather than NEWS_LOOKBACK_HOURS.
    sec_lookback_days: int = _i("SEC_LOOKBACK_DAYS", 21)

    # ── macro regime (FRED) ───────────────────────────────
    macro_enabled: bool = _b("MACRO_ENABLED", True)
    fred_api_key: str = _s("FRED_API_KEY")                 # VIX, 10y, fed funds

    # ── event-risk blackout (Finnhub calendars) ───────────
    event_blackout_enabled: bool = _b("EVENT_BLACKOUT_ENABLED", True)
    event_blackout_hours: float = _f("EVENT_BLACKOUT_HOURS", 2.0)  # no new entries within N h of a high-impact event / earnings (FOMC/CPI use per-event windows in events.py)
    event_countries: tuple[str, ...] = _csv("EVENT_COUNTRIES", "US")  # only blackout on these countries' macro prints

    # quant indicators
    sma_fast: int = _i("SMA_FAST", 20)
    sma_slow: int = _i("SMA_SLOW", 50)
    rsi_period: int = _i("RSI_PERIOD", 14)
    rsi_oversold: float = _f("RSI_OVERSOLD", 30)
    rsi_overbought: float = _f("RSI_OVERBOUGHT", 70)
    atr_period: int = _i("ATR_PERIOD", 14)

    # ── ML quant signal (LightGBM; drop-in for signals.quant_signal) ──────
    # OFF by default: the engine falls back to the indicator quant_signal until
    # a model exists AND its purged-CV AUC justifies turning this on. See train.py.
    ml_signal_enabled: bool = _b("ML_SIGNAL_ENABLED", False)
    ml_model_path: str = _s("ML_MODEL_PATH", "models/quant_lgbm.txt")
    ml_features_path: str = _s("ML_FEATURES_PATH", "models/quant_features.json")
    ml_min_prob: float = _f("ML_MIN_PROB", 0.55)   # deadband: |P-0.5| below this → FLAT
    # training / labeling
    ml_label_horizon: int = _i("ML_LABEL_HORIZON", 20)   # vertical-barrier bars
    ml_label_up_atr: float = _f("ML_LABEL_UP_ATR", 1.0)  # profit barrier = entry + k·ATR
    ml_label_dn_atr: float = _f("ML_LABEL_DN_ATR", 1.0)  # stop barrier  = entry − k·ATR
    ml_cv_splits: int = _i("ML_CV_SPLITS", 5)
    ml_num_rounds: int = _i("ML_NUM_ROUNDS", 300)
    ml_learning_rate: float = _f("ML_LEARNING_RATE", 0.03)
    ml_num_leaves: int = _i("ML_NUM_LEAVES", 31)
    ml_min_leaf: int = _i("ML_MIN_LEAF", 50)
    # ── data recorder (record-own ProjectX feed → parquet; Databento alt) ──
    record_data: bool = _b("RECORD_DATA", False)
    record_path: str = _s("RECORD_PATH", "data/recorded")

    # ── options / dealer-positioning (GEX stack) ──────────
    options_source: str = _s("OPTIONS_SOURCE", "none")     # none|cboe|flashalpha|chain (cboe=free, no key)
    flashalpha_api_key: str = _s("FLASHALPHA_API_KEY")
    # Distance (in % of spot) to treat a gamma wall as "in play".
    wall_proximity_pct: float = _f("WALL_PROXIMITY_PCT", 0.5)

    # Effective-OI calibration (v2 §2). Open interest only updates once daily, so
    # intraday GEX built from raw OI is stale. When enabled, the self-computed
    # (cboe) path estimates effective OI = OI_snapshot + weight*intraday_volume,
    # times a bounded daily residual that recalibrates the estimate against the
    # next day's observed OI.
    # ⚠ The 0.43 weight is from the v2 spec and is UNVALIDATED — default OFF until a
    #   backtest justifies it. Turning it on changes live GEX/wall placement.
    effective_oi_enabled: bool = _b("EFFECTIVE_OI_ENABLED", False)
    effective_oi_weight: float = _f("EFFECTIVE_OI_WEIGHT", 0.43)     # intraday volume → OI confidence
    effective_oi_residual_cap: float = _f("EFFECTIVE_OI_RESIDUAL_CAP", 0.5)  # residual clamped to [1-cap, 1+cap]

    # Four-Greek confluence (v2 §4). Threshold (fraction of |net_gex|) above which
    # net VEX/CHEX count as "large" for the Vanna Rally / Charm Drift reads.
    confluence_vex_threshold: float = _f("CONFLUENCE_VEX_THRESHOLD", 0.15)
    confluence_chex_threshold: float = _f("CONFLUENCE_CHEX_THRESHOLD", 0.10)
    # Live entry gate: when a symbol has a dealer-positioning read, an actionable
    # confluence ≥ min_score either boosts confidence (agrees) or vetoes the entry
    # (conflicts). Neutral/gamma-pin reads never veto a directional trade.
    confluence_gate_enabled: bool = _b("CONFLUENCE_GATE_ENABLED", True)
    confluence_min_score: float = _f("CONFLUENCE_MIN_SCORE", 0.5)   # ≥ this to act on it
    confluence_conflict_veto: bool = _b("CONFLUENCE_CONFLICT_VETO", True)
    confluence_boost: float = _f("CONFLUENCE_BOOST", 0.10)          # confidence added when agreeing

    # Regime gate (v2 §5). Backtest showed the quant edge is regime-specific: it
    # works Trending, bleeds in Crisis (−2.43%/trade, PF 0.61). Skip entries whose
    # entry-bar regime is in REGIME_BLOCK. Default blocks Crisis only.
    regime_gate_enabled: bool = _b("REGIME_GATE_ENABLED", True)
    regime_block: tuple[str, ...] = _csv("REGIME_BLOCK", "Crisis")
    # Allowlist (overrides the blocklist when set): trade ONLY these regimes.
    # REGIME_ALLOW=Trending = the only profitable config in backtest (PF 1.09).
    regime_allow: tuple[str, ...] = _csv("REGIME_ALLOW", "")
    # The equity regime gate is built for the daily swing model; the 0DTE options
    # strategy has its own GEX/confluence logic, so exempt option underlyings from
    # it — otherwise the equity regime filter starves option flow.
    regime_gate_exempt_options: bool = _b("REGIME_GATE_EXEMPT_OPTIONS", True)

    # ── 0DTE options scalper (options-primary, regime-adaptive) ──
    # When TRADE_OPTIONS=true AND a GEX regime is available, build an option
    # structure instead of an equity order; otherwise fall back to equity.
    scalp_timeframe: str = _s("SCALP_TIMEFRAME", "5Min")   # intraday bars for the quant stream
    option_strike_step: float = _f("OPTION_STRIKE_STEP", 1.0)    # listed strike increment (SPY/QQQ≈1, SPX≈5)
    option_spread_width: float = _f("OPTION_SPREAD_WIDTH", 1.0)  # width for credit spreads
    option_nominal_premium: float = _f("OPTION_NOMINAL_PREMIUM", 2.0)  # fallback $/contract if chain has no premium
    option_time_stop_et: str = _s("OPTION_TIME_STOP_ET", "15:30")      # flat all 0DTE by this ET time (charm)
    entry_cutoff_et: str = _s("ENTRY_CUTOFF_ET", "")    # no NEW entries at/after this ET time ("" = disabled); existing positions still managed/flattened
    # Only these underlyings get traded as OPTIONS (true daily/0DTE expiries live here);
    # everything else falls back to equity. SPX needs index data + cash settle.
    option_underlyings: tuple[str, ...] = _csv("OPTION_UNDERLYINGS", "SPY,QQQ,IWM")
    option_max_spread_pct: float = _f("OPTION_MAX_SPREAD_PCT", 0.20)   # skip wide bid/ask
    option_min_mid: float = _f("OPTION_MIN_MID", 0.05)                 # skip near-worthless strikes
    # Index 0DTE underlyings (SPX/XSP): cash-settled, true daily expiries. Alpaca
    # has no index data feed and can't trade index options, so for these the quant
    # DIRECTION is proxied from the tracking ETF while price/GEX/strikes come from
    # the real CBOE index chain, and fills route to the internal SimBroker.
    index_underlyings: tuple[str, ...] = _csv("INDEX_UNDERLYINGS", "")  # e.g. SPX,XSP
    index_data_proxy_raw: str = _s("INDEX_DATA_PROXY", "SPX:SPY,XSP:SPY,NDX:QQQ,RUT:IWM")

    # ── LLM (agent brains) ────────────────────────────────
    llm_enabled: bool = _b("LLM_ENABLED", True)
    llm_backend: str = _s("LLM_BACKEND", "anthropic")      # anthropic | ollama
    anthropic_api_key: str = _s("ANTHROPIC_API_KEY")
    # High-reasoning model for analysis/debate; fast model for execution checks.
    llm_model: str = _s("LLM_MODEL", "claude-sonnet-4-6")
    llm_model_deep: str = _s("LLM_MODEL_DEEP", "claude-opus-4-8")
    llm_temperature: float = _f("LLM_TEMPERATURE", 0.0)    # 0 = deterministic JSON
    llm_max_symbols_per_cycle: int = _i("LLM_MAX_SYMBOLS_PER_CYCLE", 20)
    ollama_model: str = _s("OLLAMA_MODEL", "qwen2.5:7b")
    ollama_host: str = _s("OLLAMA_HOST", "http://localhost:11434")

    # ── multi-agent team toggles ──────────────────────────
    agent_analyst: bool = _b("AGENT_ANALYST", True)        # market/news/sentiment/macro
    agent_research_debate: bool = _b("AGENT_RESEARCH_DEBATE", True)  # bull vs bear
    agent_portfolio: bool = _b("AGENT_PORTFOLIO", True)    # capital allocation
    agent_risk_manager: bool = _b("AGENT_RISK_MANAGER", True)

    # ── trading / sizing ──────────────────────────────────
    trading_mode: str = _s("TRADING_MODE", "paper")        # paper | live
    bankroll_usd: float = _f("BANKROLL_USD", 50_000)       # equity base for sizing (Topstep $50K account start)
    max_position_pct: float = _f("MAX_POSITION_PCT", 0.05)  # max 5% per name
    max_concurrent: int = _i("MAX_CONCURRENT", 20)
    min_executable_size_usd: float = _f("MIN_EXECUTABLE_SIZE_USD", 100)

    # ── Kelly position sizing ─────────────────────────────
    # Sizes by historical edge: f* = (p(b+1)-1)/b. We never bet full Kelly —
    # KELLY_FRACTION scales it (¼-Kelly default) and MAX_POSITION_PCT is a hard
    # ceiling. Falls back to conviction-only sizing until KELLY_MIN_TRADES closed
    # trades exist. KELLY_MIN_FRACTION keeps a small exploration floor so an early
    # unlucky streak (Kelly→0) can't permanently lock the bot out of sampling.
    kelly_enabled: bool = _b("KELLY_ENABLED", True)
    kelly_fraction: float = _f("KELLY_FRACTION", 0.25)          # ¼-Kelly
    kelly_min_trades: int = _i("KELLY_MIN_TRADES", 20)          # closed trades before Kelly kicks in
    kelly_min_fraction: float = _f("KELLY_MIN_FRACTION", 0.005)  # 0.5% bankroll exploration floor

    # ── exits: ATR-based stops + take-profit ──────────────
    atr_stop_mult: float = _f("ATR_STOP_MULT", 2.0)        # stop = entry - mult*ATR
    atr_target_mult: float = _f("ATR_TARGET_MULT", 3.0)    # target = entry + mult*ATR
    take_profit_pct: float = _f("TAKE_PROFIT_PCT", 0.0)    # 0 = use ATR target only
    stop_loss_pct: float = _f("STOP_LOSS_PCT", 0.08)       # hard floor regardless of ATR

    # ── safety layers ─────────────────────────────────────
    daily_drawdown_pct: float = _f("DAILY_DRAWDOWN_PCT", 0.05)   # halt for the day
    # Circuit breakers on the regime symbol's intraday move.
    cb_yellow_pct: float = _f("CB_YELLOW_PCT", 0.05)   # 5% -> halve sizes
    cb_red_pct: float = _f("CB_RED_PCT", 0.10)         # 10% -> halt new entries
    # Falling-knife protection: cooldown between trades in the same symbol.
    trade_cooldown_sec: int = _i("TRADE_COOLDOWN_SEC", 900)
    kill_switch_file: str = _s("KILL_SWITCH_FILE", "KILL_SWITCH")
    # Cramer mode: run an inverse shadow book; if it beats the real one the
    # primary signals are systematically flawed.
    cramer_mode: bool = _b("CRAMER_MODE", True)
    # Cramer flip: set True by day_learner when shadow crushes real (signals inverted).
    cramer_flip_enabled: bool = _b("CRAMER_FLIP_ENABLED", False)
    cramer_flip_threshold_usd: float = _f("CRAMER_FLIP_THRESHOLD_USD", 1000.0)

    # ── broker creds ──────────────────────────────────────
    # Futures-only fork: the live execution path is ProjectX/TopstepX (selected by
    # TOPSTEP_MODE_ENABLED + PROJECTX creds, NOT by BROKER). This base broker is
    # only the fallback the engine uses when ProjectX creds are absent, so it
    # defaults to `sim` — that keeps a clean checkout (no .env) starting in paper
    # instead of hard-failing validate() on missing Alpaca keys. Set BROKER=alpaca
    # or ibkr explicitly only if you re-enable the (stripped-out) equity path.
    broker: str = _s("BROKER", "sim")                      # sim | alpaca | ibkr
    alpaca_api_key: str = _s("ALPACA_API_KEY")
    alpaca_secret_key: str = _s("ALPACA_SECRET_KEY")
    # Paper endpoint by default; live endpoint only when TRADING_MODE=live.
    alpaca_paper_url: str = _s("ALPACA_PAPER_URL", "https://paper-api.alpaca.markets")
    alpaca_live_url: str = _s("ALPACA_LIVE_URL", "https://api.alpaca.markets")
    alpaca_data_url: str = _s("ALPACA_DATA_URL", "https://data.alpaca.markets")
    # IBKR (stub adapter for later multi-asset)
    ibkr_host: str = _s("IBKR_HOST", "127.0.0.1")
    ibkr_port: int = _i("IBKR_PORT", 7497)
    ibkr_client_id: int = _i("IBKR_CLIENT_ID", 1)

    # ── Topstep / ProjectX integration ──────────────────────
    # Enabled by default (this is the Topstep futures fork). The engine will
    # attempt to authenticate with ProjectX at startup; if credentials are absent
    # it falls back to the sim broker so the loop can still run offline.
    topstep_mode_enabled: bool = _b("TOPSTEP_MODE_ENABLED", True)
    # Order-flow confirmation gate (OBI/CVD/whale from the live ProjectX SignalR
    # feed). Only applied when a live feed has data for the symbol; fails open
    # otherwise (warm-up, non-futures symbol, mock/sim mode).
    orderflow_gate_enabled: bool = _b("ORDERFLOW_GATE_ENABLED", True)
    # Legacy Rithmic fields — kept so existing .env files with RITHMIC_* vars don't
    # error on load. The active execution path is ProjectX (below); Rithmic is unused.
    rithmic_user: str = _s("RITHMIC_USER")
    rithmic_password: str = _s("RITHMIC_PASSWORD")
    rithmic_system: str = _s("RITHMIC_SYSTEM", "Rithmic Paper Trading")
    rithmic_env: str = _s("RITHMIC_ENV", "paper")
    rithmic_url: str = _s("RITHMIC_URL", "rituz00100.rithmic.com:443")
    rithmic_app_name: str = _s("RITHMIC_APP_NAME", "JARVIS")
    rithmic_app_version: str = _s("RITHMIC_APP_VERSION", "1.0")
    # ── Topstep / ProjectX Gateway API (current execution path) ────────────
    # ProjectX is Topstep's own REST + SignalR gateway (api.topstepx.com /
    # rtc.topstepx.com). Simple API-key auth — no Rithmic app registration /
    # conformance wall. Set PROJECTX_USERNAME + PROJECTX_API_KEY to activate;
    # the order-flow engine, Topstep risk, and entry/exit logic are unchanged.
    projectx_username: str = _s("PROJECTX_USERNAME")                   # TopstepX login / username
    projectx_api_key: str = _s("PROJECTX_API_KEY")                     # API key from TopstepX → Settings → API
    projectx_api_base: str = _s("PROJECTX_API_BASE", "https://api.topstepx.com")
    projectx_rtc_base: str = _s("PROJECTX_RTC_BASE", "https://rtc.topstepx.com")
    projectx_account_name: str = _s("PROJECTX_ACCOUNT_NAME")          # blank → first tradable account
    projectx_live: bool = _b("PROJECTX_LIVE", False)                   # False = sim/eval data sub; True = funded/live
    # ── Topstep $50K funded-account rules (No-Activation-Fee + Responsible Trading) ──
    # These are the live risk constraints enforced by topstep_risk.TopstepRiskManager.
    # Defaults are the official Topstep $50K spec (2026). For $100K/$150K, override
    # via env: account_size 100000/150000, trailing_mll 3000/4500, daily_loss 2000/3000,
    # max_contracts 10/15, profit_target 6000/9000.
    topstep_account_size: float = _f("TOPSTEP_ACCOUNT_SIZE", 50_000.0)        # starting balance
    topstep_trailing_mll: float = _f("TOPSTEP_TRAILING_MLL", 2_000.0)         # trailing Max Loss Limit ($) — HARD fail rule
    topstep_profit_target: float = _f("TOPSTEP_PROFIT_TARGET", 3_000.0)       # Combine profit objective
    topstep_min_trading_days: int = _i("TOPSTEP_MIN_TRADING_DAYS", 3)         # Combine min separate active-trading days
    topstep_max_contracts: int = _i("TOPSTEP_MAX_CONTRACTS", 5)               # max ACCOUNT-WIDE open minis (50 micros @ 10:1)
    topstep_micro_ratio: int = _i("TOPSTEP_MICRO_RATIO", 10)                  # micros per 1 mini toward the limit (TopstepX)
    # Responsible Trading Advantage: adds a Daily Loss Limit. ON per the funded plan.
    topstep_responsible_trading: bool = _b("TOPSTEP_RESPONSIBLE_TRADING", True)
    topstep_daily_loss_limit: float = _f("TOPSTEP_DAILY_LOSS_LIMIT", 1_000.0)  # DLL ($) — deactivates the day
    # Profit give-back guard (OURS, not a Topstep rule): once equity has run far
    # above the locked $account_size MLL floor, Topstep offers no trailing
    # protection at all — a funded account can give back its entire accumulated
    # profit before the hard floor fires. Block NEW entries (never flattens)
    # while equity sits more than this many dollars below the cycle peak.
    # 0 = off.
    topstep_giveback_halt_usd: float = _f("TOPSTEP_GIVEBACK_HALT_USD", 2_000.0)
    # Per-trade risk budget for ATR/risk-based futures position sizing. The dollar
    # risk at the stop (qty * stop_distance_pts * $/pt) is capped to the SMALLER of
    # (pct of account) and (fraction of the Daily Loss Limit). Default: min($500, $500).
    topstep_per_trade_risk_pct: float = _f("TOPSTEP_PER_TRADE_RISK_PCT", 0.01)            # 1% of $50k = $500
    topstep_per_trade_risk_dll_frac: float = _f("TOPSTEP_PER_TRADE_RISK_DLL_FRAC", 0.5)   # ≤ 50% of the DLL
    # Consistency rule (payout eligibility): best single day ≤ this fraction of
    # cumulative profit. Topstep: 50%. Enforced as a per-day profit cap that stops
    # NEW entries once today's profit reaches consistency_pct * profit_target.
    topstep_consistency_pct: float = _f("TOPSTEP_CONSISTENCY_PCT", 0.50)
    # Topstep does NOT ban scalping (unlike the old Topstep ≤5s rule). The microscalp
    # guard below is therefore OFF by default; flip on only if your firm reinstates it.
    topstep_scalp_guard_enabled: bool = _b("TOPSTEP_SCALP_GUARD_ENABLED", False)

    # ── flow-risk overlays (flow_risk.py): vol-target sizing + toxicity veto ──
    # Research-validated (research/gamma_rv_precheck.py). Self-calibrating against
    # each symbol's own bar history. A10 vol sizing is folded into the futures
    # risk multiplier, which futures_plan caps at 1.0 -> on a funded account it
    # can only DE-RISK in elevated-vol regimes, never size up (the cap value is
    # therefore only exercised on the equities/USD path, which is dormant here).
    vol_sizing_enabled: bool = _b("VOL_SIZING_ENABLED", True)
    vol_target_ratio: float = _f("VOL_TARGET_RATIO", 1.0)      # 1.0 = target the symbol's own normal vol
    vol_sizing_floor: float = _f("VOL_SIZING_FLOOR", 0.34)     # min multiplier (elevated vol -> size down)
    vol_sizing_cap: float = _f("VOL_SIZING_CAP", 2.0)          # max multiplier (equities path only; futures clamps to 1.0)
    # A8 toxicity veto: stand aside when BVC-VPIN sits in the top tail of the
    # symbol's own VPIN history. Risk filter ONLY, never a directional signal
    # (Andersen-Bondarenko: VPIN's content is largely mechanical vol/volume).
    toxicity_veto_enabled: bool = _b("TOXICITY_VETO_ENABLED", True)
    toxicity_pct_threshold: float = _f("TOXICITY_PCT_THRESHOLD", 0.90)  # veto when VPIN in top decile
    toxicity_min_bars: int = _i("TOXICITY_MIN_BARS", 60)       # need this much history before vetoing
    vpin_window_bars: int = _i("VPIN_WINDOW_BARS", 20)         # rolling window for the VPIN calc

    # Session timing + econ blackout for the Topstep risk layer.
    topstep_flatten_time: str = _s("TOPSTEP_FLATTEN_TIME", "16:08")        # flatten all by this ET (before 16:10 futures close)
    topstep_econ_blackout_min: int = int(os.getenv("TOPSTEP_ECON_BLACKOUT_MIN", "5"))  # blackout window around econ releases (minutes)
    # Microscalping guard — DORMANT under Topstep (topstep_scalp_guard_enabled=False).
    # Kept for the optional ≤Ns profit-share attribution + unit tests. When the guard
    # is enabled: (1) hold profit-target exits until open ≥ topstep_min_profit_hold_sec
    # (stop-losses are NEVER delayed); (2) block NEW entries once ≤Ns winners exceed
    # topstep_scalp_profit_pct_limit of realized profit.
    topstep_min_profit_hold_sec: float = _f("TOPSTEP_MIN_PROFIT_HOLD_SEC", 5.0)
    topstep_scalp_profit_pct_limit: float = _f("TOPSTEP_SCALP_PROFIT_PCT_LIMIT", 0.40)
    # Signal-only bridge: when ProjectX credentials are absent the bot runs on the
    # sim broker and emits copy-paste TRADE/EXIT tickets for the user to execute
    # manually on the TopstepX dashboard. Set MANUAL_TICKETS=false once live API
    # execution is confirmed working (no need for hand-off tickets then).
    manual_tickets: bool = _b("MANUAL_TICKETS", True)
    # Futures symbols to watch when Topstep mode is active (comma-separated roots)
    futures_symbols: tuple[str, ...] = _csv("FUTURES_SYMBOLS", "ES,NQ,MES,MNQ")

    # ── yfinance enrichment ───────────────────────────────
    # Set YFINANCE_ENABLED=false to disable all yfinance calls (no-key fallback path).
    yfinance_enabled: bool = _b("YFINANCE_ENABLED", True)
    # Skip entering any position when the next earnings date is within this many
    # calendar days (earnings vol is unpredictable; the existing Finnhub blackout
    # covers the tight ±12h window; this adds a longer forward-looking guard).
    skip_earnings_window_days: int = _i("SKIP_EARNINGS_WINDOW_DAYS", 5)
    # All yfinance responses are cached in-process for this many minutes to avoid
    # hitting rate limits on a large watchlist.
    yf_cache_ttl_min: int = _i("YF_CACHE_TTL_MIN", 15)
    # IV rank thresholds (0-100) for options structure selection:
    #   rank > sell_threshold  → prefer credit spreads (sell elevated premium)
    #   rank < buy_threshold   → prefer debit structures (buy cheap vol)
    #   in between             → let the GEX regime decide (existing logic)
    iv_rank_sell_threshold: float = _f("IV_RANK_SELL_THRESHOLD", 50.0)
    iv_rank_buy_threshold:  float = _f("IV_RANK_BUY_THRESHOLD",  30.0)

    # ── Unusual Whales flow integration ──────────────────
    uw_api_key: str = _s("UW_API_KEY")
    uw_flow_enabled: bool = _b("UW_FLOW_ENABLED", False)
    # Weight of UW lean blended into the quant signal (0.30 = 30% UW, 70% indicators).
    uw_flow_lean_weight: float = _f("UW_FLOW_LEAN_WEIGHT", 0.30)
    # Futures→proxy map override ("ES:SPX,NQ:NDX,MES:SPX,MNQ:NDX"); defaults built in uw_flow.py.
    uw_proxy_map_raw: str = _s("UW_PROXY_MAP", "")
    uw_flow_limit: int = _i("UW_FLOW_LIMIT", 50)              # flow tickets to fetch per cycle
    uw_flow_cache_sec: int = _i("UW_FLOW_CACHE_SEC", 120)     # TTL before re-fetching
    uw_whale_premium_usd: float = _f("UW_WHALE_PREMIUM_USD", 500_000.0)  # $500K+ = whale block
    # Correlation logger: append (ts, symbol, spot, uw_lean, quant_lean) per cycle
    # to this CSV so UW's predictive value can be measured offline (behavior-neutral,
    # never affects trades). Empty path = disabled. Analyze with `python uw_logger.py`.
    uw_flow_log: str = _s("UW_FLOW_LOG", "")

    # ── notify / logging ──────────────────────────────────
    discord_webhook: str = _s("DISCORD_WEBHOOK")
    log_file: str = _s("LOG_FILE", "signals.log")

    @property
    def is_live(self) -> bool:
        return self.trading_mode.lower() == "live"

    @property
    def alpaca_base_url(self) -> str:
        return self.alpaca_live_url if self.is_live else self.alpaca_paper_url

    @property
    def news_sources(self) -> tuple[str, ...]:
        """Resolved, lower-cased source list. Falls back to the legacy single
        NEWS_SOURCE when NEWS_SOURCES is unset. 'none' anywhere disables news."""
        srcs = tuple(s.lower() for s in self.news_sources_raw) or (self.news_source.lower(),)
        if "none" in srcs:
            return ()
        return srcs

    def _source_ready(self, src: str) -> bool:
        """Whether a single source can actually fetch (has any key it needs)."""
        if src in ("google", "rss", "sec"):
            return True               # free, no key
        if src == "polygon":
            return bool(self.polygon_api_key)
        if src == "finnhub":
            return bool(self.finnhub_api_key)
        if src == "alpaca":
            return bool(self.alpaca_api_key and self.alpaca_secret_key)
        if src == "unusualwhales":
            return bool(self.uw_api_key)
        return False

    @property
    def news_ready(self) -> bool:
        """Ready if news is on and at least one configured source can fetch."""
        if not self.news_enabled:
            return False
        return any(self._source_ready(s) for s in self.news_sources)

    @property
    def llm_ready(self) -> bool:
        if not self.llm_enabled:
            return False
        if self.llm_backend == "ollama":
            return True
        return bool(self.anthropic_api_key)

    def validate(self) -> list[str]:
        """Return list of fatal config problems (empty = ok)."""
        errs: list[str] = []
        if self.broker not in ("alpaca", "ibkr", "sim"):
            errs.append("BROKER must be alpaca|ibkr|sim")
        # ProjectX credentials: warn when Topstep mode is on but keys are absent
        # (not fatal — the engine falls back to sim broker until credentials are set).
        if self.topstep_mode_enabled and not (self.projectx_username and self.projectx_api_key):
            print(
                "WARNING: TOPSTEP_MODE_ENABLED=True but PROJECTX_USERNAME or PROJECTX_API_KEY "
                "is empty — engine will fall back to sim broker until credentials are set."
            )
        # The LIVE Topstep path executes through ProjectX (TopstepX), NOT Rithmic.
        # When Topstep mode is armed for live/funded trading (PROJECTX_LIVE=True or
        # TRADING_MODE=live), the ProjectX credentials MUST be present — otherwise
        # the broker silently degrades to MOCK mode and a "live" config places no
        # real orders (and never enforces the trailing MLL against the exchange).
        if self.topstep_mode_enabled and (self.projectx_live or self.is_live):
            if not self.projectx_username or not self.projectx_api_key:
                errs.append(
                    "TOPSTEP_MODE_ENABLED + live (PROJECTX_LIVE/TRADING_MODE=live) requires "
                    "PROJECTX_USERNAME and PROJECTX_API_KEY — refusing to run 'live' in mock mode"
                )
        # PROJECTX_LIVE selects the funded ProjectX contract/account universe;
        # TRADING_MODE is the operator's top-level paper/live intent. The two
        # must agree — ProjectXBroker.submit() places real orders whenever
        # credentials are valid regardless of TRADING_MODE, so
        # PROJECTX_LIVE=true + TRADING_MODE=paper is not a safe "paper test of
        # the live account", it is a live account with the operator believing
        # otherwise. Refuse to start rather than let that combination run.
        if self.topstep_mode_enabled and self.projectx_live and not self.is_live:
            errs.append(
                "PROJECTX_LIVE=true (funded ProjectX environment) but TRADING_MODE=paper — "
                "refusing to start with this contradictory config. Set TRADING_MODE=live to "
                "confirm you intend to trade the funded account, or PROJECTX_LIVE=false for "
                "sim/eval."
            )
        # Live trading with no persisted state means a restart reseeds
        # peak_equity from the current balance instead of the true trailing
        # high-water mark, silently loosening the Topstep MLL floor.
        if self.is_live and not os.getenv("DATABASE_URL", "").strip():
            errs.append(
                "TRADING_MODE=live but DATABASE_URL is empty — live trading requires "
                "persisted state (peak_equity / day-halt) to survive a restart; refusing "
                "to start stateless. Set DATABASE_URL or keep TRADING_MODE=paper."
            )
        if self.llm_backend not in ("anthropic", "ollama"):
            errs.append("LLM_BACKEND must be anthropic|ollama")
        if self.llm_enabled and self.llm_backend == "anthropic" and not self.anthropic_api_key:
            errs.append("LLM_ENABLED (anthropic) but ANTHROPIC_API_KEY is empty")
        if self.uw_flow_enabled and not self.uw_api_key:
            errs.append("UW_FLOW_ENABLED=true but UW_API_KEY is empty")
        if not 0.0 <= self.uw_flow_lean_weight <= 1.0:
            errs.append("UW_FLOW_LEAN_WEIGHT must be in [0, 1]")
        if self.min_confidence not in ("low", "medium", "high"):
            errs.append("MIN_CONFIDENCE must be low|medium|high")
        if not 0 < self.max_position_pct <= 1:
            errs.append("MAX_POSITION_PCT must be in (0,1]")
        if self.broker == "alpaca" and not (self.alpaca_api_key and self.alpaca_secret_key):
            errs.append("BROKER=alpaca needs ALPACA_API_KEY + ALPACA_SECRET_KEY")
        if self.is_live and self.broker == "alpaca" and not self.alpaca_api_key:
            errs.append("TRADING_MODE=live but Alpaca creds missing")
        if self.trade_options and self.options_source == "none":
            errs.append("TRADE_OPTIONS=true but OPTIONS_SOURCE=none (no dealer-positioning data)")
        if self.options_source == "flashalpha" and not self.flashalpha_api_key:
            errs.append("OPTIONS_SOURCE=flashalpha but FLASHALPHA_API_KEY is empty")
        if not self.watchlist:
            errs.append("WATCHLIST is empty")
        _valid_news = {"google", "rss", "polygon", "finnhub", "alpaca", "sec", "unusualwhales", "none"}
        bad = [s for s in self.news_sources if s not in _valid_news]
        if bad:
            errs.append(f"NEWS_SOURCES has unknown source(s): {','.join(bad)} "
                        f"(valid: {'|'.join(sorted(_valid_news))})")
        if "rss" in self.news_sources and "{q}" not in self.news_rss_template:
            errs.append("NEWS_SOURCES includes rss but NEWS_RSS_TEMPLATE has no {q} placeholder")
        # news degrades gracefully (a keyless source just yields no headlines), so a
        # missing key is a warning, not fatal.
        if self.news_enabled:
            for s in self.news_sources:
                if not self._source_ready(s):
                    print(f"WARNING: NEWS_SOURCES includes '{s}' but its key is missing — "
                          "that source yields no headlines until set")
        return errs

    def is_index(self, sym: str) -> bool:
        """True for cash-settled index underlyings (SPX/XSP) that need proxy data
        + Sim execution rather than Alpaca."""
        return sym.upper() in self.index_underlyings

    def proxy_for(self, sym: str) -> str:
        """ETF whose bars stand in for an index's direction (SPX->SPY). Returns the
        symbol unchanged when it has no mapping."""
        u = sym.upper()
        for pair in self.index_data_proxy_raw.split(","):
            k, _, v = pair.partition(":")
            if k.strip().upper() == u and v.strip():
                return v.strip().upper()
        return sym


CONFIG = Config()
