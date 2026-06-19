"""Typed config loaded from environment / .env.

Stock + options trading bot (2026 agentic framework). Equities trade live in
paper mode via Alpaca; the options-exposure (GEX/DEX/VEX/CHEX) stack is wired
but needs a dealer-positioning data source to produce live numbers.
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
    watchlist: tuple[str, ...] = _csv("WATCHLIST", "AAPL,MSFT,NVDA,SPY,QQQ,TSLA,AMZN,META")
    # Universe index used for regime / circuit-breaker reads.
    regime_symbol: str = _s("REGIME_SYMBOL", "SPY")

    # ── asset classes enabled ─────────────────────────────
    trade_equities: bool = _b("TRADE_EQUITIES", True)
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
    # policy REQUIRES a descriptive User-Agent with a contact email; set yours here.
    sec_user_agent: str = _s("SEC_USER_AGENT", "jarvis-stock jackson.g.sheehan@gmail.com")
    sec_form_types: tuple[str, ...] = _csv("SEC_FORM_TYPES", "8-K")  # filing types to surface
    # Filings are sparse and stay material far longer than a news headline, so they
    # use their own (much longer) lookback rather than NEWS_LOOKBACK_HOURS.
    sec_lookback_days: int = _i("SEC_LOOKBACK_DAYS", 21)

    # ── macro regime (FRED) ───────────────────────────────
    macro_enabled: bool = _b("MACRO_ENABLED", True)
    fred_api_key: str = _s("FRED_API_KEY")                 # VIX, 10y, fed funds

    # ── event-risk blackout (Finnhub calendars) ───────────
    event_blackout_enabled: bool = _b("EVENT_BLACKOUT_ENABLED", True)
    event_blackout_hours: float = _f("EVENT_BLACKOUT_HOURS", 12.0)  # no new entries within N h of a high-impact event / earnings
    event_countries: tuple[str, ...] = _csv("EVENT_COUNTRIES", "US")  # only blackout on these countries' macro prints

    # quant indicators
    sma_fast: int = _i("SMA_FAST", 20)
    sma_slow: int = _i("SMA_SLOW", 50)
    rsi_period: int = _i("RSI_PERIOD", 14)
    rsi_oversold: float = _f("RSI_OVERSOLD", 30)
    rsi_overbought: float = _f("RSI_OVERBOUGHT", 70)
    atr_period: int = _i("ATR_PERIOD", 14)

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

    # ── broker creds ──────────────────────────────────────
    broker: str = _s("BROKER", "alpaca")                   # alpaca | ibkr | sim
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

    # ── Lucid Trading / Rithmic integration ───────────────
    # DISABLED by default. Set LUCID_MODE_ENABLED=True AND supply
    # RITHMIC_USER + RITHMIC_PASSWORD to activate futures execution
    # through the Rithmic executor + Lucid risk layer.
    lucid_mode_enabled: bool = _b("LUCID_MODE_ENABLED", True)  # this is the Lucid futures fork
    # Order-flow confirmation gate (OBI/CVD/whale from the live Rithmic L1+trade
    # feed). Only applied when a live feed has data for the symbol; fails open
    # otherwise (warm-up, non-futures symbol, mock mode).
    orderflow_gate_enabled: bool = _b("ORDERFLOW_GATE_ENABLED", True)
    # Rithmic credentials — leave blank until you have your account.
    rithmic_user: str = _s("RITHMIC_USER")                             # your Rithmic username
    rithmic_password: str = _s("RITHMIC_PASSWORD")                     # your Rithmic password
    rithmic_system: str = _s("RITHMIC_SYSTEM", "Rithmic Paper Trading")  # system/gateway name
    rithmic_env: str = _s("RITHMIC_ENV", "paper")                      # "paper" | "live"
    rithmic_url: str = _s("RITHMIC_URL", "rituz00100.rithmic.com:443")  # WebSocket gateway URL (test default; production URL provided by Rithmic after conformance)
    # Rithmic ties each credential set to a REGISTERED app_name/app_version
    # (issued with API access / after conformance). A wrong one → login rp_code 13
    # "permission denied". Set these to exactly what Rithmic/Lucid assigned you.
    rithmic_app_name: str = _s("RITHMIC_APP_NAME", "JARVIS")
    rithmic_app_version: str = _s("RITHMIC_APP_VERSION", "1.0")
    # ── Topstep / ProjectX Gateway API (current execution path) ────────────
    # ProjectX is Topstep's own REST + SignalR gateway (api.topstepx.com /
    # rtc.topstepx.com). Simple API-key auth — no Rithmic app registration /
    # conformance wall. Set PROJECTX_USERNAME + PROJECTX_API_KEY to activate;
    # the order-flow engine, Lucid risk, and entry/exit logic are unchanged.
    projectx_username: str = _s("PROJECTX_USERNAME")                   # TopstepX login / username
    projectx_api_key: str = _s("PROJECTX_API_KEY")                     # API key from TopstepX → Settings → API
    projectx_api_base: str = _s("PROJECTX_API_BASE", "https://api.topstepx.com")
    projectx_rtc_base: str = _s("PROJECTX_RTC_BASE", "https://rtc.topstepx.com")
    projectx_account_name: str = _s("PROJECTX_ACCOUNT_NAME")          # blank → first tradable account
    projectx_live: bool = _b("PROJECTX_LIVE", False)                   # False = sim/eval data sub; True = funded/live
    # ── Topstep $50K funded-account rules (No-Activation-Fee + Responsible Trading) ──
    # These are the live risk constraints enforced by lucid_risk.TopstepRiskManager.
    # Defaults are the official Topstep $50K spec (2026). For $100K/$150K, override
    # via env: account_size 100000/150000, trailing_mll 3000/4500, daily_loss 2000/3000,
    # max_contracts 10/15, profit_target 6000/9000.
    topstep_account_size: float = _f("TOPSTEP_ACCOUNT_SIZE", 50_000.0)        # starting balance
    topstep_trailing_mll: float = _f("TOPSTEP_TRAILING_MLL", 2_000.0)         # trailing Max Loss Limit ($) — HARD fail rule
    topstep_profit_target: float = _f("TOPSTEP_PROFIT_TARGET", 3_000.0)       # Combine profit objective
    topstep_max_contracts: int = _i("TOPSTEP_MAX_CONTRACTS", 5)               # max ACCOUNT-WIDE open minis (50 micros @ 10:1)
    topstep_micro_ratio: int = _i("TOPSTEP_MICRO_RATIO", 10)                  # micros per 1 mini toward the limit (TopstepX)
    # Responsible Trading Advantage: adds a Daily Loss Limit. ON per the funded plan.
    topstep_responsible_trading: bool = _b("TOPSTEP_RESPONSIBLE_TRADING", True)
    topstep_daily_loss_limit: float = _f("TOPSTEP_DAILY_LOSS_LIMIT", 1_000.0)  # DLL ($) — deactivates the day
    # Consistency rule (payout eligibility): best single day ≤ this fraction of
    # cumulative profit. Topstep: 50%. Enforced as a per-day profit cap that stops
    # NEW entries once today's profit reaches consistency_pct * profit_target.
    topstep_consistency_pct: float = _f("TOPSTEP_CONSISTENCY_PCT", 0.50)
    # Topstep does NOT ban scalping (unlike the old Lucid ≤5s rule). The microscalp
    # guard below is therefore OFF by default; flip on only if your firm reinstates it.
    topstep_scalp_guard_enabled: bool = _b("TOPSTEP_SCALP_GUARD_ENABLED", False)

    # Lucid risk parameters (retuned for Topstep $50K; legacy names kept for the
    # risk layer + tests). lucid_daily_drawdown_usd now mirrors the DLL.
    lucid_daily_drawdown_usd: float = _f("LUCID_DAILY_DRAWDOWN_USD", 1_000.0)  # = Topstep daily loss limit ($)
    lucid_max_contracts: int = _i("LUCID_MAX_CONTRACTS", 5)            # account-wide max open contracts
    lucid_flatten_time: str = _s("LUCID_FLATTEN_TIME", "16:08")        # flatten all by this ET (before 16:10 futures close)
    lucid_econ_blackout_min: int = int(os.getenv("LUCID_ECON_BLACKOUT_MIN", "5"))  # blackout window around econ releases (minutes)
    # Microscalping guard — DORMANT under Topstep (topstep_scalp_guard_enabled=False).
    # Kept for the optional ≤Ns profit-share attribution + unit tests. When the guard
    # is enabled: (1) hold profit-target exits until open ≥ lucid_min_profit_hold_sec
    # (stop-losses are NEVER delayed); (2) block NEW entries once ≤Ns winners exceed
    # lucid_scalp_profit_pct_limit of realized profit.
    lucid_min_profit_hold_sec: float = _f("LUCID_MIN_PROFIT_HOLD_SEC", 5.0)
    lucid_scalp_profit_pct_limit: float = _f("LUCID_SCALP_PROFIT_PCT_LIMIT", 0.40)
    # Signal-only bridge: until the bot has direct Rithmic API access, it runs on
    # the Sim broker and emits copy-paste TRADE/EXIT tickets for the user to
    # execute manually on the Tradesea dashboard. Set MANUAL_TICKETS=false once
    # live API execution is wired (no need for hand-off tickets then).
    manual_tickets: bool = _b("MANUAL_TICKETS", True)
    # Futures symbols to watch when Lucid mode is active (comma-separated roots)
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
        # Lucid / Rithmic: warn when enabled but missing credentials (not fatal —
        # the engine falls back to Alpaca automatically in that case).
        if self.lucid_mode_enabled and not (self.rithmic_user and self.rithmic_password):
            print(
                "WARNING: LUCID_MODE_ENABLED=True but RITHMIC_USER or RITHMIC_PASSWORD "
                "is empty — engine will fall back to Alpaca until credentials are set."
            )
        if self.lucid_mode_enabled and self.rithmic_env not in ("paper", "live"):
            errs.append("RITHMIC_ENV must be 'paper' or 'live'")
        if self.llm_backend not in ("anthropic", "ollama"):
            errs.append("LLM_BACKEND must be anthropic|ollama")
        if self.llm_enabled and self.llm_backend == "anthropic" and not self.anthropic_api_key:
            errs.append("LLM_ENABLED (anthropic) but ANTHROPIC_API_KEY is empty")
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
        _valid_news = {"google", "rss", "polygon", "finnhub", "alpaca", "sec", "none"}
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
