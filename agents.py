"""Multi-agent trading team (LangGraph-style decomposition, plain Python).

Pipeline per symbol:

    Analyst      — fuse market structure + news/sentiment + macro into a
                   qualitative BUY/HOLD/SELL lean with a 0..1 conviction.
    Researchers  — bull vs bear debate that critiques the analyst read and
                   returns an adjusted conviction (optional).
    Portfolio    — allocates conviction-weighted size across candidates.
    Risk Manager — final structural veto (exposure, regime, walls).

Execution lives in executor.py. Every LLM call uses temperature 0 and demands
strict JSON (the doc's reliability rule). With no LLM configured the agents
degrade to neutral so the deterministic quant stream still runs the bot.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from config import CONFIG

# Dealer-positioning exposure is an equity/options concept; the futures bot never
# populates it (ctx.exposure stays None), but the field + None-guards are kept so
# the agent prompts stay identical to the sister equity bot.
ExposureProfile = object

# ── LLM client ────────────────────────────────────────────


class _OllamaBlock:
    """Mimics an anthropic content block (.text) so callers stay unchanged."""
    def __init__(self, text: str):
        self.text = text


class _OllamaResponse:
    def __init__(self, text: str):
        self.content = [_OllamaBlock(text)]


class _OllamaMessages:
    """anthropic-compatible .create() backed by local Ollama /api/chat.

    Ignores the requested model (deep/fast both map to OLLAMA_MODEL) and forces
    JSON output at the requested temperature.
    """
    def __init__(self, host: str):
        self._http = httpx.Client(base_url=host, timeout=CONFIG.llm_timeout_sec)

    def create(self, *, model=None, max_tokens=400, temperature=0.0, messages):
        r = self._http.post("/api/chat", json={
            "model": CONFIG.ollama_model,
            "messages": messages,
            "stream": False,
            "format": "json",
            "options": {"temperature": temperature, "num_predict": max_tokens},
        })
        r.raise_for_status()
        return _OllamaResponse(r.json().get("message", {}).get("content", ""))


class OllamaClient:
    """Local, free LLM backend. No API key, no per-call cost."""
    def __init__(self, host: str):
        self.messages = _OllamaMessages(host)


def _build_client():
    """Return an LLM client for the configured backend, or None when unusable."""
    if not CONFIG.llm_enabled:
        return None
    if CONFIG.llm_backend == "ollama":
        try:
            return OllamaClient(CONFIG.ollama_host)
        except Exception:  # noqa: BLE001
            return None
    if CONFIG.llm_backend == "anthropic" and CONFIG.anthropic_api_key:
        try:
            import anthropic

            return anthropic.Anthropic(
                api_key=CONFIG.anthropic_api_key, timeout=CONFIG.llm_timeout_sec)
        except Exception:  # noqa: BLE001
            return None
    return None


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _ask_json(client, prompt: str, *, deep: bool = False, max_tokens: int = 400) -> dict:
    """Single structured-JSON LLM turn. Returns {} on any failure."""
    if client is None:
        return {}
    model = CONFIG.llm_model_deep if deep else CONFIG.llm_model
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=CONFIG.llm_temperature,
            messages=[{"role": "user", "content": prompt}],
            # prefill an opening brace to force clean JSON out of the model
        )
        text = "".join(getattr(b, "text", "") for b in msg.content)
    except Exception:  # noqa: BLE001
        return {}
    m = _JSON_RE.search(text)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


# ── context passed into the team ──────────────────────────


@dataclass
class SymbolContext:
    symbol: str
    spot: float
    quant_detail: str        # human-readable indicator read
    quant_lean: float        # -1..1
    news: list[str] = field(default_factory=list)   # recent headlines (optional)
    exposure: ExposureProfile | None = None         # dealer-positioning map
    macro: str = ""          # one-line macro/regime note
    # ── yfinance enrichment (optional) ───────────────────
    analyst_rec: str = ""           # 'strong buy'|'buy'|'hold'|'sell'|'strong sell'
    analyst_target: float | None = None   # analyst mean target price
    yf_news: list[str] = field(default_factory=list)  # top-3 yfinance headlines


@dataclass
class AgentVerdict:
    qual_lean: float = 0.0           # -1..1 qualitative direction
    conviction: float = 0.0          # 0..1
    thesis: str = ""
    trail: dict = field(default_factory=dict)  # per-agent decisions for the UI

    @property
    def direction(self) -> str:
        return "BUY" if self.qual_lean > 0 else ("SELL" if self.qual_lean < 0 else "HOLD")


class AgentTeam:
    def __init__(self):
        self.client = _build_client()

    @property
    def ready(self) -> bool:
        return self.client is not None

    # ── Analyst: market + news + macro fusion ─────────────
    def analyst(self, ctx: SymbolContext) -> dict:
        if not CONFIG.agent_analyst or not self.client:
            return {"lean": 0.0, "conviction": 0.0, "thesis": "analyst off/neutral"}
        gex = ctx.exposure.summary() if ctx.exposure else "no options-positioning data"
        news = "\n".join(f"- {h}" for h in ctx.news[:8]) or "- (no headlines supplied)"

        # ── yfinance enrichment sections (omitted when empty) ──
        analyst_section = ""
        if ctx.analyst_rec:
            tp = f", mean target ${ctx.analyst_target:.2f}" if ctx.analyst_target else ""
            analyst_section = f"ANALYST CONSENSUS: {ctx.analyst_rec.upper()}{tp}\n"

        yf_news_section = ""
        if ctx.yf_news:
            yf_lines = "\n".join(f"- {h}" for h in ctx.yf_news[:3])
            yf_news_section = f"RECENT HEADLINES (yfinance):\n{yf_lines}\n"

        prompt = (
            "You are the Analyst agent on a quant trading desk. Fuse the signals "
            "into a single directional read. Respond ONLY with JSON: "
            '{"lean": <-1..1>, "conviction": <0..1>, "thesis": "<=140 chars"}.\n\n'
            f"SYMBOL: {ctx.symbol}  SPOT: {ctx.spot}\n"
            f"QUANT (technical): {ctx.quant_detail} (lean {ctx.quant_lean:+.2f})\n"
            f"DEALER POSITIONING: {gex}\n"
            f"MACRO: {ctx.macro or 'n/a'}\n"
            f"{analyst_section}"
            f"{yf_news_section}"
            f"NEWS:\n{news}\n"
        )
        out = _ask_json(self.client, prompt)
        return {
            "lean": float(out.get("lean", 0.0)),
            "conviction": float(out.get("conviction", 0.0)),
            "thesis": str(out.get("thesis", ""))[:160],
        }

    # ── Researchers: bull vs bear debate ──────────────────
    def debate(self, ctx: SymbolContext, analyst: dict) -> dict:
        if not CONFIG.agent_research_debate or not self.client:
            return analyst
        prompt = (
            "Two researchers debate the Analyst's call, one bullish one bearish. "
            "Weigh both and return the desk's adjusted view. Respond ONLY with JSON: "
            '{"lean": <-1..1>, "conviction": <0..1>, "thesis": "<=140 chars"}.\n\n'
            f"SYMBOL: {ctx.symbol}\n"
            f"ANALYST: lean {analyst['lean']:+.2f} conviction {analyst['conviction']:.2f} "
            f"— {analyst['thesis']}\n"
            f"QUANT: {ctx.quant_detail}\n"
            f"DEALER: {ctx.exposure.summary() if ctx.exposure else 'n/a'}\n"
        )
        out = _ask_json(self.client, prompt)
        if not out:
            return analyst
        return {
            "lean": float(out.get("lean", analyst["lean"])),
            "conviction": float(out.get("conviction", analyst["conviction"])),
            "thesis": str(out.get("thesis", analyst["thesis"]))[:160],
        }

    # ── Risk Manager: structural veto ─────────────────────
    def risk_manager(self, ctx: SymbolContext, view: dict) -> tuple[bool, str]:
        """Deterministic structural checks first; they never need the LLM.

        Vetoes a long into a call wall / short into a put wall, and longs in a
        negative-gamma regime sitting below the gamma flip (amplified downside).
        """
        if not CONFIG.agent_risk_manager:
            return True, "risk-mgr off"
        exp = ctx.exposure
        lean = view["lean"]
        if exp:
            wall = exp.near_wall(CONFIG.wall_proximity_pct)
            if lean > 0 and wall == "call_wall":
                return False, "long blocked: at call wall (resistance)"
            if lean < 0 and wall == "put_wall":
                return False, "short blocked: at put wall (support)"
            if lean > 0 and exp.regime == "negative-gamma" and exp.gamma_flip and exp.spot < exp.gamma_flip:
                return False, "long blocked: neg-gamma below flip (amplified downside)"
        return True, "risk ok"

    # ── full pipeline for one symbol ──────────────────────
    def evaluate(self, ctx: SymbolContext) -> AgentVerdict:
        trail: dict = {}
        analyst = self.analyst(ctx)
        trail["analyst"] = round(analyst["lean"], 2)

        view = self.debate(ctx, analyst)
        trail["debate"] = round(view["lean"], 2)

        ok, reason = self.risk_manager(ctx, view)
        trail["risk"] = ok
        if not ok:
            return AgentVerdict(qual_lean=0.0, conviction=0.0, thesis=reason, trail=trail)

        return AgentVerdict(
            qual_lean=max(-1.0, min(1.0, view["lean"])),
            conviction=max(0.0, min(1.0, view["conviction"])),
            thesis=view["thesis"],
            trail=trail,
        )


# ── Portfolio agent: conviction-weighted sizing ───────────


def portfolio_weights(candidates: list[tuple[str, float]]) -> dict[str, float]:
    """Allocate fractional weights across (symbol, conviction) candidates.

    Simple conviction-proportional split, normalised to 1.0. The Portfolio
    agent in the doc outputs JSON weights; this is the deterministic floor —
    swap in an LLM allocator later if you want regime-aware tilts.
    """
    if not candidates:
        return {}
    # Only the strongest MAX_CONCURRENT candidates actually open — allocate across THOSE,
    # not the whole field, or a big universe starves every position below the min size.
    ranked = sorted(candidates, key=lambda x: max(0.0, x[1]), reverse=True)[:CONFIG.max_concurrent]
    if not CONFIG.agent_portfolio:
        return {sym: 1.0 / len(ranked) for sym, _ in ranked}
    total = sum(max(0.0, c) for _, c in ranked)
    if total <= 0:
        return {}
    return {sym: max(0.0, c) / total for sym, c in ranked}
