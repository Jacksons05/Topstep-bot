"""The agentic trading cycle: data -> quant -> agents -> confluence -> risk ->
execute -> manage. One run_once() = one full pass over the watchlist.

Confluence rule (the doc's core): a trade fires only when the quantitative
stream (indicators) and the qualitative stream (agent team) agree on direction
AND the blended confidence clears CONFIDENCE_THRESHOLD.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

from agents import AgentTeam, SymbolContext, portfolio_weights
from config import CONFIG
from executor import build_executor
from marketdata import MarketData
from news import NewsFeed
from events import Events
from macro import Macro
from notifier import notify, signal_msg
from options import cboe_chain, exposure_for, four_greek_confluence
from regime import classify_last
from options_strategy import directional_levels, price_structure, select_structure
from risk import check, circuit_breaker, kill_switch_active, should_exit
from signals import Signal, label_for, quant_signal
from state import State
import yf_data
from regime_strategy import (
    get_regime_params,
    regime_allows_signal,
    apply_regime_sizing,
)


# US equity-market full-day holidays (NYSE), 2026. Used by the market-hours gate
# so the LLM scan doesn't burn API credits on closed days.
_US_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}

# Time-stop option exits go through Alpaca, which intermittently returns a 500
# ("internal server error") on otherwise-valid close orders. A single transient
# 500 must not permanently strand a position, but we also can't re-submit every
# scan tick (~30 s) forever. Retry up to this many times across scans, then park
# the position until the next restart and stop hitting the broker.
_OPTION_EXIT_MAX_RETRIES = 5


class Engine:
    def __init__(self):
        self.data = MarketData()
        self.news = NewsFeed()
        self.macro = Macro()
        self.events = Events()
        self.team = AgentTeam()
        self.executor = build_executor()
        self.state = State.load()
        self.cooldowns: dict[str, float] = {}
        self.cb_level = "green"
        # cost control: skip re-running the LLM on a symbol whose quant read hasn't
        # moved since its last evaluation. {symbol: (direction, strength_bucket)}
        self._eval_cache: dict[str, tuple[str, float]] = {}
        self._lucid_last_reset: date | None = None
        # Track time-stop exit retry counts so a transient Alpaca 500 is retried
        # a bounded number of times across scans, then parked instead of spamming
        # the broker every 30 s. Keyed by the position's DB identity
        # (symbol, opened_at) so the count survives state reloads / object churn.
        self._option_exit_attempts: dict[tuple[str, str], int] = {}

        # ── Lucid / Rithmic mode gate ──────────────────────────────────────
        # When LUCID_MODE_ENABLED=True AND Rithmic credentials are provided,
        # swap the broker for RithmicBroker and attach the Lucid risk layer.
        # Otherwise the engine runs in standard Alpaca mode — no other changes.
        self._lucid: "LucidRiskManager | None" = None  # type: ignore[name-defined]
        if CONFIG.lucid_mode_enabled and CONFIG.rithmic_user and CONFIG.rithmic_password:
            try:
                from rithmic_executor import RithmicBroker
                from lucid_risk import LucidRiskManager
                self.executor.broker = RithmicBroker()
                # Seed the Lucid drawdown base from the live account balance.
                # Falls back to bankroll_usd if the account call fails (mock mode).
                try:
                    acct = self.executor.broker.account()
                    initial_equity = acct.get("equity") or CONFIG.bankroll_usd
                except Exception:  # noqa: BLE001
                    initial_equity = CONFIG.bankroll_usd
                self._lucid = LucidRiskManager(initial_equity=initial_equity)
                if CONFIG.trade_options:
                    notify("⚠ LUCID MODE: TRADE_OPTIONS auto-disabled (Rithmic has no options executor)")
                    CONFIG.trade_options = False  # type: ignore[assignment]
                notify(
                    "⚡ LUCID/RITHMIC MODE ACTIVE — "
                    f"system={CONFIG.rithmic_system} env={CONFIG.rithmic_env} | "
                    f"drawdown_limit=${CONFIG.lucid_daily_drawdown_usd:,.2f} | "
                    f"max_contracts={CONFIG.lucid_max_contracts} | "
                    f"flatten_at={CONFIG.lucid_flatten_time} ET"
                )
            except Exception as e:  # noqa: BLE001
                notify(
                    f"⚠ LUCID MODE init failed ({e}) — "
                    "falling back to Alpaca. Check rithmic-python installation."
                )
                self._lucid = None
        elif CONFIG.lucid_mode_enabled:
            # Enabled in config but no credentials — warn loudly, keep Alpaca.
            notify(
                "⚠ LUCID_MODE_ENABLED=True but RITHMIC_USER/PASSWORD not set — "
                "running in standard Alpaca mode. Set credentials in .env to activate."
            )
        else:
            notify("ℹ LUCID MODE disabled — standard Alpaca/Sim mode active")

    def _market_open(self) -> bool:
        """True during US regular trading hours (Mon–Fri 9:30–16:00 ET, skip
        holidays). Always True when MARKET_HOURS_ONLY is off."""
        if not CONFIG.market_hours_only:
            return True
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
        if now.weekday() >= 5 or now.date().isoformat() in _US_HOLIDAYS:
            return False
        mins = now.hour * 60 + now.minute
        return 9 * 60 + 30 <= mins < 16 * 60

    def close(self) -> None:
        self.data.close()
        self.news.close()
        self.macro.close()
        self.events.close()
        self.executor.close_broker()
        self.state.save()

    # ── adaptive cadence: poll faster when the breaker trips, idle when closed ──
    def next_interval(self) -> int:
        if not self._market_open():
            return CONFIG.closed_interval_sec
        return CONFIG.fast_interval_sec if self.cb_level != "green" else CONFIG.scan_interval_sec

    # ── one full cycle ────────────────────────────────────
    def run_once(self) -> None:
        # Cost gate: outside market hours, do nothing (no fills happen, no LLM spend).
        if not self._market_open():
            notify("market closed — idle (no scan)")
            return

        # Refresh Lucid drawdown base at the start of each new trading day
        if self._lucid is not None:
            today = date.today()
            if self._lucid_last_reset != today:
                try:
                    acct = self.executor.broker.account()
                    self._lucid.reset_day(acct.get("equity", CONFIG.bankroll_usd))
                    self._lucid_last_reset = today
                except Exception:  # noqa: BLE001
                    pass

        # 0. reconcile provisional entries against real broker fills
        self._reconcile_fills()

        # 1. regime + circuit breaker
        regime_move = self.data.intraday_change_pct(CONFIG.regime_symbol)
        self.cb_level, cb_mult = circuit_breaker(regime_move)

        # 2. manage exits first (real + shadow books)
        self._manage_open()

        # 3. two-stage funnel so the universe can be large without LLM cost exploding.
        held = {p.symbol for p in self.state.open_positions}

        # 3a. cheap quant prescreen across the WHOLE watchlist (no LLM)
        prescreened = []  # (sym, quant, spot, regime_label)
        for sym in CONFIG.watchlist:
            if sym in held:
                continue
            ps = self._prescreen(sym)
            if ps is not None:
                prescreened.append((sym, ps[0], ps[1], ps[2]))
        # rank by quant conviction; only the strongest get the expensive LLM/GEX pass
        prescreened.sort(key=lambda x: x[1].strength, reverse=True)
        top = prescreened[:CONFIG.llm_max_symbols_per_cycle]
        # always let option underlyings reach full eval (GEX + structure), even if a
        # large equity universe outranks them on quant strength — else options starve.
        if CONFIG.trade_options:
            have = {s for s, _, _, _ in top}
            top += [r for r in prescreened
                    if r[0] in CONFIG.option_underlyings and r[0] not in have]

        # 3b. full eval (LLM + GEX + option structure) on the top-N candidates only
        candidates: list[tuple[Signal, float, dict]] = []  # (signal, conviction, regime_params)
        regime_str = "—"
        for sym, quant, spot, regime_label in top:
            # cost control: skip the LLM team when this symbol's quant read hasn't
            # moved since we last evaluated it (same direction + strength bucket).
            key = (quant.direction, round(quant.strength / 0.05) * 0.05)
            if self._eval_cache.get(sym) == key:
                continue
            self._eval_cache[sym] = key
            sig = self._evaluate_full(sym, quant, spot)
            if sig:
                rp = get_regime_params(regime_label, CONFIG)
                candidates.append((sig, sig.confidence, rp))

        # surface options regime if any candidate carried an exposure read
        for sig, _, _rp in candidates:
            if sig.agents.get("regime"):
                regime_str = sig.agents["regime"]
                break

        notify(f"scan: {len(CONFIG.watchlist)} universe | "
               f"prescreen={len(prescreened)} scored={len(top)} | "
               f"open={len(self.state.open_positions)} | "
               f"realized=${self.state.realized_pnl_usd:.2f} | "
               f"dayPnL=${self.state.daily_pnl():.2f} | "
               f"cb={self.cb_level} | regime={regime_str}")

        # 4. portfolio allocation across agreeing candidates
        weights = portfolio_weights([(s.symbol, c) for s, c, _ in candidates])

        # 5. risk gate + execute. Options-primary: option signals take slots FIRST so a
        # big equity universe can't crowd the few 0DTE underlyings out of MAX_CONCURRENT.
        if candidates and self._past_entry_cutoff():
            notify(f"  skip ALL new entries (entry cutoff {CONFIG.entry_cutoff_et} ET) — "
                   f"{len(candidates)} candidate(s); managing open positions only")
            candidates = []
        for sig, conv, regime_params in sorted(
                candidates,
                key=lambda x: (x[0].asset == "option", x[1]),
                reverse=True):
            w = weights.get(sig.symbol, 0.0)

            # ── Regime-adaptive playbook gates ──────────────────────────
            rk = regime_params.get("regime_key", "UNKNOWN")

            # (a) Direction gate — e.g. Crisis blocks new longs
            dir_ok, dir_reason = regime_allows_signal(regime_params, sig.side)
            if not dir_ok:
                notify(f"  skip ({dir_reason}): {sig.symbol}")
                continue

            # (b) Per-regime minimum confidence (stricter than global threshold)
            regime_min_conf = regime_params.get("min_conf", CONFIG.confidence_threshold)
            if sig.confidence < regime_min_conf:
                notify(f"  skip (regime {rk} conf {sig.confidence:.2f} < {regime_min_conf:.2f}): "
                       f"{sig.symbol}")
                continue

            # (c) Per-regime concurrent-position cap (e.g. Crisis: max 2)
            regime_max_pos = regime_params.get("max_positions", CONFIG.max_concurrent)
            if len(self.state.open_positions) >= regime_max_pos:
                notify(f"  skip (regime {rk} max_positions={regime_max_pos} reached): "
                       f"{sig.symbol}")
                continue

            black, why = self.events.blackout(sig.symbol)
            if black:
                notify(f"  skip (event blackout: {why}): {sig.symbol}")
                continue
            # Lucid risk layer: EOD flatten window, econ blackout, drawdown,
            # and contract cap — all checked BEFORE the base risk.check() call.
            if self._lucid is not None:
                lucid_ok, lucid_reason = self._lucid.pre_trade_ok(sig, self.state)
                if not lucid_ok:
                    notify(f"  skip (Lucid: {lucid_reason}): {sig.symbol}")
                    continue
            ok, size, reason = check(sig, self.state, cb_mult=cb_mult * (w or 1.0),
                                     cooldowns=self.cooldowns)
            if not ok:
                notify(f"  skip ({reason}): {sig.symbol}")
                continue

            # (d) Regime sizing multiplier applied AFTER the base risk.check()
            #     so the min-size guard still runs against the un-scaled base.
            size = apply_regime_sizing(size, regime_params)
            if size < CONFIG.min_executable_size_usd:
                notify(f"  skip (regime {rk} sized to {size:.0f} < min "
                       f"{CONFIG.min_executable_size_usd:.0f}): {sig.symbol}")
                continue

            qty = max(1, int(size / sig.price)) if sig.price > 0 else 0
            try:
                self.executor.open(sig, size, self.state)
            except Exception as e:  # noqa: BLE001
                notify(f"  ! execute failed {sig.symbol}: {e}")
                continue
            self.cooldowns[sig.symbol] = time.time()
            notify("OPEN  " + signal_msg(sig, qty, size, self.executor.mode) +
                   f" [regime={rk} sz={regime_params['size_mult']:.0%}]")

        self.state.save()

    # ── stage 1: cheap quant prescreen (no LLM/GEX/news) ──
    def _prescreen(self, sym: str):
        """Fast pass run on the WHOLE universe: bars + indicators only.

        Returns (quant, spot, regime_label) for a passable read, else None.
        No LLM, no network beyond bars.

        Regime gate (v2 §5, regime-adaptive update):
        - REGIME_ALLOW in .env acts as an allowlist; symbols whose regime is
          not in the list are dropped ONLY when the allowlist contains exactly
          one entry (the old hard-block behaviour for back-compat).
        - When REGIME_ALLOW contains more than one entry (e.g. all four
          regimes), every allowed regime passes through and the per-regime
          playbook is applied later in run_once() — so sizing / confidence /
          direction restrictions are regime-adaptive rather than binary.
        - The hard REGIME_BLOCK still applies for explicitly blocked regimes.
        - Option underlyings remain exempt (their own GEX/confluence logic).
        """
        tf = CONFIG.scalp_timeframe if CONFIG.trade_options else "1Day"
        bars = self.data.bars(sym, timeframe=tf, limit=200)
        if not bars.get("close"):
            return None
        spot = bars["close"][-1]
        quant = quant_signal(bars)
        if quant is None:
            return None
        # Option underlyings reach full eval even on a FLAT quant: in a pinned
        # (positive-gamma) tape momentum vanishes, but that's exactly where the
        # 0DTE credit-spread edge lives — direction is derived from dealer
        # positioning (spot vs gamma flip) in _evaluate_full instead.
        opt_underlying = CONFIG.trade_options and sym in CONFIG.option_underlyings
        if quant.direction == "FLAT" and not opt_underlying:
            return None

        # Classify regime for this symbol (used both by the gate and later by
        # the regime-adaptive playbook in run_once).
        option_sym = (CONFIG.trade_options and sym in CONFIG.option_underlyings
                      and CONFIG.regime_gate_exempt_options)
        regime_label = "Unknown"
        if CONFIG.regime_gate_enabled and not option_sym:
            regime_label = classify_last(
                bars["close"],
                bars.get("high") or bars["close"],
                bars.get("low") or bars["close"],
            )
            reg_upper = regime_label.upper()
            if CONFIG.regime_allow:
                if reg_upper not in CONFIG.regime_allow:
                    # Regime is not in the allowlist — drop regardless of how
                    # many entries the allowlist has.  When all four regimes
                    # are listed (the new default) this branch is never reached.
                    return None
            elif CONFIG.regime_block and reg_upper in CONFIG.regime_block:
                return None

        return quant, spot, regime_label

    # ── stage 2: expensive full eval (LLM + GEX + structure) on top-N ──
    def _gex_option_signal(self, sym: str, spot: float, exp, atr: float,
                           ivr: float | None):
        """Build a 0DTE option Signal from dealer positioning alone (no momentum).

        Direction = spot vs gamma flip: above flip → bullish lean (bull-put credit /
        long call), below → bearish. Used only for option underlyings whose momentum
        quant is FLAT but whose GEX regime is live. Returns a priced+liquid option
        Signal, or None when no valid structure exists.
        """
        direction = "BUY" if spot >= exp.gamma_flip else "SELL"
        # Confidence scales with distance from the flip (conviction of the lean),
        # normalised by ATR. Floored at 0.5, gated by the same threshold as equities.
        dist = abs(spot - exp.gamma_flip)
        confidence = round(min(1.0, 0.5 + dist / (atr * 4.0)), 3) if atr else 0.55
        if confidence < CONFIG.confidence_threshold:
            return None

        # Four-Greek confluence veto: never enter a structure dealer flow mechanically
        # opposes (mirrors the equity path's conflict veto).
        conf = None
        if CONFIG.confluence_gate_enabled:
            conf = four_greek_confluence(exp, zero_dte=exp.front_expiry == date.today())
            if conf.actionable and conf.score >= CONFIG.confluence_min_score:
                want = {"bullish": "BUY", "bearish": "SELL"}.get(conf.direction or "")
                if want and want != direction and CONFIG.confluence_conflict_veto:
                    notify(f"  skip (GEX dir {direction} vs confluence {conf.direction} "
                           f"{conf.playbook} {conf.score}): {sym}")
                    return None

        chain = cboe_chain(sym, spot)
        if chain is None:
            return None
        structure = select_structure(
            exp, direction, exp.front_expiry,
            strike_step=exp.strike_step, spread_width=exp.strike_step,
            iv_rank=ivr,
            iv_rank_sell_threshold=CONFIG.iv_rank_sell_threshold,
            iv_rank_buy_threshold=CONFIG.iv_rank_buy_threshold,
        )
        if structure is None or not price_structure(
                chain, structure, spread_width=exp.strike_step,
                max_spread_pct=CONFIG.option_max_spread_pct, min_mid=CONFIG.option_min_mid):
            return None

        agents = {"regime": exp.regime, "gex_dir": f"spot {spot:.2f} vs flip {exp.gamma_flip:.2f}"}
        if conf and conf.actionable:
            agents["confluence"] = f"{conf.playbook}:{conf.direction}:{conf.score}"
        if ivr is not None:
            agents["iv_rank"] = round(ivr, 1)
        stop, target = directional_levels(direction, spot, exp, atr)
        structure.target = target
        sig = Signal(
            symbol=sym, asset="option", side=direction, price=spot,
            confidence=confidence, confidence_label=label_for(confidence),
            thesis=structure.thesis or f"GEX {exp.regime} 0DTE {direction}",
            quant=0.0, qual=0.0, atr=atr, agents=agents,
            stop=stop, structure=structure,
            contract=structure.legs[0].occ if structure.legs else "",
        )
        return sig

    def _evaluate_full(self, sym: str, quant, spot: float) -> Signal | None:
        # Use the LIVE quote for the trade spot (entry ref, ATM strike, stop/target).
        # The last bar close can lag/stale; greeks + levels must anchor to real price.
        q = self.data.quote(sym)
        if q and q.price > 0:
            spot = q.price

        # ── 1. yfinance earnings guard ────────────────────
        # Skip the position entirely when earnings are within SKIP_EARNINGS_WINDOW_DAYS.
        # This runs before the LLM/GEX calls so a near-earnings symbol costs nothing.
        # (The existing Finnhub blackout in events.py covers the tighter ±12h window;
        #  this adds a longer forward-looking guard and works without a Finnhub key.)
        earn_soon, earn_reason = yf_data.earnings_within_days(sym)
        if earn_soon:
            notify(f"  skip (earnings window: {earn_reason}): {sym}")
            return None

        exp = exposure_for(sym, spot) if CONFIG.trade_options or CONFIG.options_source != "none" else None

        # ── 2. yfinance enrichment (cached, best-effort) ──
        # Fetch IV rank, analyst consensus, and yfinance news.  All three degrade
        # gracefully to None / empty strings so a yfinance outage is never fatal.
        ivr       = yf_data.iv_rank(sym)
        ac        = yf_data.analyst_consensus(sym)
        yf_heads  = yf_data.news_headlines(sym, n=3)

        # ── GEX-direction path: option underlying, live regime, FLAT momentum ──
        # In a pinned (positive-gamma) tape the momentum quant is FLAT, so the normal
        # confluence path (which requires a quant direction) never fires — yet that's
        # where the 0DTE credit-spread edge lives. Derive direction from dealer
        # positioning (spot vs gamma flip) and build the structure directly. Bypasses
        # the equity quant/LLM confluence; still risk-defined + liquidity-validated.
        if (quant.direction == "FLAT" and CONFIG.trade_options
                and sym in CONFIG.option_underlyings and exp is not None
                and exp.regime != "neutral" and exp.gamma_flip):
            gex = self._gex_option_signal(sym, spot, exp, quant.atr, ivr)
            if gex is not None:
                return gex
            return None  # FLAT underlying with no buildable structure → no equity fallback

        # Headlines only matter when an LLM agent will read them — skip the
        # Polygon call on the quant-only path to save rate limit.
        headlines = self.news.headlines(sym) if self.team.ready else []

        ctx = SymbolContext(
            symbol=sym, spot=spot, quant_detail=quant.detail, quant_lean=quant.lean,
            exposure=exp, news=headlines,
            macro=self.macro.line() if self.team.ready else "",
            # ── yfinance fields ──
            analyst_rec=ac.get("recommendation", ""),
            analyst_target=ac.get("target_price"),
            yf_news=yf_heads,
        )
        verdict = self.team.evaluate(ctx)

        # When no LLM is configured the qualitative lean is 0 — fall back to a
        # quant-only signal so the bot still trades (paper) while you wire keys.
        if not self.team.ready:
            qual_dir = quant.direction
            qual_conv = quant.strength
        else:
            qual_dir = verdict.direction
            qual_conv = verdict.conviction
            if qual_dir == "HOLD":
                return None

        # confluence: directions must agree
        if qual_dir != quant.direction:
            return None

        confidence = round(0.5 * quant.strength + 0.5 * qual_conv, 3)

        # Four-Greek confluence gate (v2 §4): dealer positioning boosts an agreeing
        # entry and vetoes one it mechanically opposes. Runs before the threshold so
        # a boost can lift a borderline trade and a conflict can kill it outright.
        conf = None
        if exp is not None and CONFIG.confluence_gate_enabled:
            zero_dte = exp.front_expiry == date.today()
            conf = four_greek_confluence(exp, zero_dte=zero_dte)
            if conf.actionable and conf.score >= CONFIG.confluence_min_score:
                want = {"bullish": "BUY", "bearish": "SELL"}.get(conf.direction or "")
                if want == quant.direction:
                    confidence = round(min(1.0, confidence + CONFIG.confluence_boost), 3)
                elif want and CONFIG.confluence_conflict_veto:
                    notify(f"  skip (confluence {conf.playbook} {conf.direction} "
                           f"vs {quant.direction}, score {conf.score}): {sym}")
                    return None

        if confidence < CONFIG.confidence_threshold:
            return None

        agents = dict(verdict.trail)
        if exp:
            agents["regime"] = exp.regime
        if conf and conf.actionable:
            agents["confluence"] = f"{conf.playbook}:{conf.direction}:{conf.score}"
        # Stash yfinance enrichment in the agents dict for the dashboard / notifier.
        if ivr is not None:
            agents["iv_rank"] = round(ivr, 1)
        if ac.get("recommendation"):
            agents["analyst_rec"] = ac["recommendation"]
        if ac.get("target_price"):
            agents["analyst_target"] = ac["target_price"]

        # quant-only mode has no narrative thesis — surface the indicator read.
        thesis = quant.detail if not self.team.ready else (verdict.thesis or quant.detail)
        sig = Signal(
            symbol=sym, asset="equity", side=quant.direction, price=spot,
            confidence=confidence, confidence_label=label_for(confidence),
            thesis=thesis, quant=quant.lean,
            qual=verdict.qual_lean, atr=quant.atr, agents=agents,
        )

        # ── 3. Options-primary: 0DTE structure, now IV-rank aware ────────────
        # On a 0DTE-capable underlying with a live regime, build a priced+liquid
        # structure.  IV rank (when available) may override the regime-based
        # debit/credit choice.  Anything missing falls back to equity.
        if (CONFIG.trade_options and exp is not None and exp.regime != "neutral"
                and exp.front_expiry is not None and sym in CONFIG.option_underlyings):
            chain = cboe_chain(sym, spot)
            structure = select_structure(
                exp, quant.direction, exp.front_expiry,
                strike_step=exp.strike_step, spread_width=exp.strike_step,
                # ── IV rank override ──
                iv_rank=ivr,
                iv_rank_sell_threshold=CONFIG.iv_rank_sell_threshold,
                iv_rank_buy_threshold=CONFIG.iv_rank_buy_threshold,
            ) if chain is not None else None
            if structure is not None and price_structure(
                    chain, structure, spread_width=exp.strike_step,
                    max_spread_pct=CONFIG.option_max_spread_pct, min_mid=CONFIG.option_min_mid):
                sig.asset = "option"
                sig.structure = structure
                sig.contract = structure.legs[0].occ if structure.legs else ""
                sig.thesis = structure.thesis or thesis
                # underlying stop/target on the correct side of spot (else instant exit)
                stop, target = directional_levels(quant.direction, spot, exp, quant.atr)
                sig.stop = stop
                structure.target = target
        return sig

    # ── reconcile provisional entries with real fills ─────
    def _reconcile_fills(self) -> None:
        """Provisional entries (recorded at the reference price when the order
        was merely 'accepted', e.g. placed after hours) get rewritten to the
        true fill price once the broker reports it. ATR stop/target distances
        are preserved by shifting them with the entry. Dead orders are dropped.
        """
        for pos in self.state.open_positions:  # real book only; shadow has no order
            if pos.filled or not pos.order_id or pos.order_id == "sim":
                continue
            # Option positions track the UNDERLYING as entry_price; the option fill price
            # would corrupt that. Just mark filled — option P&L is premium-based at close.
            if pos.asset == "option":
                status, _ = self.executor.get_fill(pos.order_id)
                if status in ("canceled", "expired", "rejected"):
                    pos.open = False
                    pos.closed_at = datetime.now(timezone.utc).isoformat()
                    notify(f"VOID option {pos.symbol} order {status}")
                elif status == "filled":
                    pos.filled = True
                continue
            try:
                status, price = self.executor.get_fill(pos.order_id)
            except Exception:  # noqa: BLE001
                continue
            if status in ("canceled", "expired", "rejected"):
                pos.open = False
                pos.filled = True
                pos.exit_price = pos.entry_price
                pos.pnl_usd = 0.0
                pos.closed_at = datetime.now(timezone.utc).isoformat()
                notify(f"VOID {pos.symbol} order {status} — provisional entry dropped")
                continue
            if status == "filled" and price:
                s_off = pos.stop - pos.entry_price
                t_off = pos.target - pos.entry_price
                pos.entry_price = price
                pos.stop = price + s_off
                pos.target = price + t_off
                pos.size_usd = pos.qty * price
                pos.filled = True
                notify(f"FILL {pos.symbol} @ {price:.2f} (reconciled from provisional)")

    def _past_option_time_stop(self) -> bool:
        """True once we're at/after OPTION_TIME_STOP_ET (charm-unwind window)."""
        from zoneinfo import ZoneInfo
        try:
            hh, mm = (int(x) for x in CONFIG.option_time_stop_et.split(":"))
        except (ValueError, AttributeError):
            return False
        now = datetime.now(ZoneInfo("America/New_York"))
        return (now.hour, now.minute) >= (hh, mm)

    def _past_entry_cutoff(self) -> bool:
        """True once we're at/after ENTRY_CUTOFF_ET. Blocks NEW entries only;
        open positions are still managed and flattened normally."""
        from zoneinfo import ZoneInfo
        try:
            hh, mm = (int(x) for x in CONFIG.entry_cutoff_et.split(":"))
        except (ValueError, AttributeError):
            return False
        now = datetime.now(ZoneInfo("America/New_York"))
        return (now.hour, now.minute) >= (hh, mm)

    # ── manage exits ──────────────────────────────────────
    def _manage_open(self) -> None:
        charm_flat = self._past_option_time_stop()

        # Lucid EOD flatten: if the flatten window is open and the broker is
        # RithmicBroker, submit a market close for every open futures position.
        # This runs BEFORE the per-position exit loop so the state gets cleaned
        # up correctly on the same scan tick.
        if self._lucid is not None and self._lucid.should_flatten_now():
            from rithmic_executor import RithmicBroker
            if isinstance(self.executor.broker, RithmicBroker):
                open_futures = [
                    p for p in self.state.open_positions
                    if not p.shadow and p.asset != "option"
                ]
                if open_futures:
                    notify(
                        f"[Lucid] EOD flatten triggered ({len(open_futures)} positions) "
                        f"— submitting market closes via Rithmic"
                    )
                    try:
                        self.executor.broker.flatten_all()
                        for pos in open_futures:
                            q = self.data.quote(pos.symbol)
                            exit_price = q.price if q else pos.entry_price
                            self.state.close(pos, exit_price)
                            notify(
                                f"  LUCID-FLATTEN {pos.symbol} qty={pos.qty} "
                                f"@ ~{exit_price:.2f}"
                            )
                    except Exception as e:  # noqa: BLE001
                        notify(f"  ! Lucid flatten error: {e}")

        for pos in list(self.state.positions):
            if not pos.open:
                continue
            if not pos.filled:
                continue  # entry order still pending — nothing to exit yet
            q = self.data.quote(pos.symbol)
            if not q:
                continue
            # 0DTE charm time-stop: flatten all option positions by OPTION_TIME_STOP_ET.
            # Guards:
            #  • self._market_open() — don't attempt after 4 PM / before 9:30 AM
            #  • bounded retry — Alpaca 500s are usually transient, so retry up to
            #    _OPTION_EXIT_MAX_RETRIES times across scans before parking the
            #    position, instead of either giving up on the first error or
            #    hammering the broker every 30 s forever.
            if charm_flat and self._market_open() and pos.asset == "option" and not pos.shadow:
                key = (pos.symbol, pos.opened_at)
                attempts = self._option_exit_attempts.get(key, 0)
                if attempts < _OPTION_EXIT_MAX_RETRIES:
                    try:
                        self.executor.close(pos, q.price, self.state)
                        notify(f"  TIME-STOP (3:30pm charm): {pos.symbol} {pos.kind}")
                        self._option_exit_attempts.pop(key, None)  # clear on success
                    except Exception as e:  # noqa: BLE001
                        attempts += 1
                        self._option_exit_attempts[key] = attempts
                        if attempts < _OPTION_EXIT_MAX_RETRIES:
                            notify(f"  ! time-stop exit failed {pos.symbol} "
                                   f"(attempt {attempts}/{_OPTION_EXIT_MAX_RETRIES}): {e} — will retry")
                        else:
                            notify(f"  ! time-stop exit failed {pos.symbol} "
                                   f"(attempt {attempts}/{_OPTION_EXIT_MAX_RETRIES}): {e} — "
                                   f"parked until restart")
                continue
            reason = should_exit(pos, q.price)
            if not reason:
                continue
            try:
                self.executor.close(pos, q.price, self.state)
            except Exception as e:  # noqa: BLE001
                notify(f"  ! exit failed {pos.symbol}: {e}")
                continue
            tag = "EXIT-SHADOW" if pos.shadow else "CLOSE"
            notify(f"{tag} {reason} {pos.symbol} @ {q.price:.2f} pnl=${pos.pnl_usd:.2f}")
