"""News + sentiment feed. Multi-source, aggregated per symbol (NEWS_SOURCES).

Sources (listed in NEWS_SOURCES, pulled and merged round-robin, deduped):

  google  — Google News RSS search (free, no key, default baseline)
  rss     — any custom RSS feed; NEWS_RSS_TEMPLATE with a {q} ticker placeholder
  polygon — Polygon Ticker News (needs key) — adds pre-labeled per-ticker sentiment
  finnhub — Finnhub company-news (needs key) — headline + summary, no label
  alpaca  — Alpaca News API (uses existing Alpaca creds) — Benzinga-sourced, ticker-tagged
  sec     — SEC EDGAR 8-K filings (free, no key) — material-event headlines straight from filings
  none    — disabled

Only polygon carries a per-article sentiment label; for every other source the
analyst infers tone from the headline (+ summary) text itself.

Best-effort: any single source failing returns an empty list, so a cycle never
dies on news and the surviving sources still feed the analyst.
"""
from __future__ import annotations

import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import httpx

from config import CONFIG

_POLY_BASE = "https://api.polygon.io"
_SENT_SIGN = {"positive": 1, "negative": -1, "neutral": 0}

# SEC EDGAR ticker→CIK map (one fetch, cached for the process lifetime).
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_ATOM = "{http://www.w3.org/2005/Atom}"
_sec_cik_cache: dict[str, str] = {}        # TICKER -> 10-digit CIK
_sec_cik_fetched: float = 0.0
_SEC_CIK_TTL = 3600 * 24                    # refresh the ticker map at most daily


@dataclass
class NewsItem:
    title: str
    publisher: str
    published_utc: str
    sentiment: str          # positive | negative | neutral | ""
    reasoning: str = ""

    def as_prompt_line(self) -> str:
        tag = f"[{self.sentiment}] " if self.sentiment else ""
        src = f" ({self.publisher})" if self.publisher else ""
        return f"{tag}{self.title}{src}"


class NewsFeed:
    def __init__(self, timeout: float = 10.0):
        # Shared client; per-source auth (Alpaca keys, SEC UA, Polygon bearer) is
        # passed per-request since the sources are now mixed within one feed.
        self._http = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "jarvis-stock/1.0"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._http.close()

    @property
    def ready(self) -> bool:
        return CONFIG.news_ready

    # ── aggregation entry point ───────────────────────────
    def fetch(self, symbol: str) -> list[NewsItem]:
        """Pull every configured source, merge round-robin, dedup by title, cap."""
        if not self.ready:
            return []
        per_source: list[list[NewsItem]] = []
        for src in CONFIG.news_sources:
            if not CONFIG._source_ready(src):
                continue
            items = self._fetch_one(src, symbol)
            if items:
                per_source.append(items)
        return self._merge(per_source)

    def _fetch_one(self, src: str, symbol: str) -> list[NewsItem]:
        if src in ("google", "rss"):
            return self._fetch_rss(symbol)
        if src == "polygon":
            return self._fetch_polygon(symbol)
        if src == "finnhub":
            return self._fetch_finnhub(symbol)
        if src == "alpaca":
            return self._fetch_alpaca(symbol)
        if src == "sec":
            return self._fetch_sec(symbol)
        return []

    @staticmethod
    def _merge(per_source: list[list[NewsItem]]) -> list[NewsItem]:
        """Round-robin interleave so no single source dominates, dedup on title,
        cap at NEWS_MAX_HEADLINES."""
        out: list[NewsItem] = []
        seen: set[str] = set()
        cap = CONFIG.news_max_headlines
        for i in range(max((len(s) for s in per_source), default=0)):
            for src in per_source:
                if i >= len(src):
                    continue
                item = src[i]
                key = item.title.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                out.append(item)
                if len(out) >= cap:
                    return out
        return out

    # ── RSS (Google News / custom) ────────────────────────
    def _fetch_rss(self, symbol: str) -> list[NewsItem]:
        url = CONFIG.news_rss_template.format(q=urllib.parse.quote(symbol))
        try:
            r = self._http.get(url)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:  # noqa: BLE001
            return []
        items: list[NewsItem] = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            src_el = item.find("source")
            publisher = (src_el.text.strip() if src_el is not None and src_el.text else "")
            # Google News titles are "Headline - Source"; strip the trailing source.
            if not publisher and " - " in title:
                title, publisher = title.rsplit(" - ", 1)
            items.append(NewsItem(
                title=title.strip(), publisher=publisher,
                published_utc=(item.findtext("pubDate") or "").strip(), sentiment="",
            ))
            if len(items) >= CONFIG.news_per_symbol:
                break
        return items

    # ── Polygon (keyed, sentiment-labeled) ────────────────
    def _fetch_polygon(self, symbol: str) -> list[NewsItem]:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=CONFIG.news_lookback_hours)).isoformat()
        try:
            r = self._http.get(
                f"{_POLY_BASE}/v2/reference/news",
                params={"ticker": symbol, "order": "desc", "sort": "published_utc",
                        "published_utc.gte": cutoff, "limit": CONFIG.news_per_symbol},
                headers={"Authorization": f"Bearer {CONFIG.polygon_api_key}"},
            )
            r.raise_for_status()
            results = r.json().get("results") or []
        except Exception:  # noqa: BLE001
            return []
        items: list[NewsItem] = []
        for a in results:
            sentiment, reasoning = "", ""
            for ins in a.get("insights") or []:
                if (ins.get("ticker") or "").upper() == symbol.upper():
                    sentiment = (ins.get("sentiment") or "").lower()
                    reasoning = ins.get("sentiment_reasoning") or ""
                    break
            items.append(NewsItem(
                title=(a.get("title") or "").strip(),
                publisher=((a.get("publisher") or {}).get("name") or "").strip(),
                published_utc=a.get("published_utc") or "",
                sentiment=sentiment, reasoning=reasoning,
            ))
        return items

    # ── Finnhub (keyed: structured company news + summaries) ──
    def _fetch_finnhub(self, symbol: str) -> list[NewsItem]:
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(hours=CONFIG.news_lookback_hours)).date().isoformat()
        try:
            r = self._http.get(
                "https://finnhub.io/api/v1/company-news",
                params={"symbol": symbol, "from": frm, "to": now.date().isoformat(),
                        "token": CONFIG.finnhub_api_key},
            )
            r.raise_for_status()
            results = r.json() or []
        except Exception:  # noqa: BLE001
            return []
        items: list[NewsItem] = []
        for a in results[: CONFIG.news_per_symbol]:
            title = (a.get("headline") or "").strip()
            if not title:
                continue
            ts = a.get("datetime")
            published = (datetime.fromtimestamp(ts, timezone.utc).isoformat()
                         if isinstance(ts, (int, float)) and ts else "")
            # Free tier carries no per-article label; analyst infers tone from the
            # headline + summary (same path as RSS). Summary rides along as reasoning.
            items.append(NewsItem(
                title=title, publisher=(a.get("source") or "").strip(),
                published_utc=published, sentiment="",
                reasoning=(a.get("summary") or "").strip()[:200],
            ))
        return items

    # ── Alpaca News API (Benzinga-sourced, uses existing Alpaca creds) ──
    def _fetch_alpaca(self, symbol: str) -> list[NewsItem]:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=CONFIG.news_lookback_hours)).isoformat()
        try:
            r = self._http.get(
                f"{CONFIG.alpaca_data_url}/v1beta1/news",
                params={"symbols": symbol, "start": start,
                        "limit": CONFIG.news_per_symbol, "sort": "desc"},
                headers={"APCA-API-KEY-ID": CONFIG.alpaca_api_key,
                         "APCA-API-SECRET-KEY": CONFIG.alpaca_secret_key},
            )
            r.raise_for_status()
            results = r.json().get("news") or []
        except Exception:  # noqa: BLE001
            return []
        items: list[NewsItem] = []
        for a in results[: CONFIG.news_per_symbol]:
            title = (a.get("headline") or "").strip()
            if not title:
                continue
            # No per-article sentiment label; summary rides along for the analyst.
            items.append(NewsItem(
                title=title, publisher=(a.get("source") or "").strip(),
                published_utc=(a.get("created_at") or "").strip(), sentiment="",
                reasoning=(a.get("summary") or "").strip()[:200],
            ))
        return items

    # ── SEC EDGAR 8-K filings (free, no key) ──────────────
    def _cik_for(self, symbol: str) -> str | None:
        """Resolve ticker → zero-padded 10-digit CIK from SEC's ticker map (cached)."""
        global _sec_cik_fetched
        now = time.time()
        if not _sec_cik_cache or now - _sec_cik_fetched > _SEC_CIK_TTL:
            try:
                r = self._http.get(_SEC_TICKERS_URL,
                                   headers={"User-Agent": CONFIG.sec_user_agent})
                r.raise_for_status()
                for row in (r.json() or {}).values():
                    t = str(row.get("ticker", "")).upper()
                    cik = row.get("cik_str")
                    if t and cik is not None:
                        _sec_cik_cache[t] = f"{int(cik):010d}"
                _sec_cik_fetched = now
            except Exception:  # noqa: BLE001
                pass    # keep whatever map we had; resolution just misses this cycle
        return _sec_cik_cache.get(symbol.upper())

    def _fetch_sec(self, symbol: str) -> list[NewsItem]:
        cik = self._cik_for(symbol)
        if not cik:
            return []
        form = (CONFIG.sec_form_types[0] if CONFIG.sec_form_types else "8-K")
        try:
            r = self._http.get(
                "https://www.sec.gov/cgi-bin/browse-edgar",
                params={"action": "getcompany", "CIK": cik, "type": form,
                        "owner": "include", "count": CONFIG.news_per_symbol,
                        "output": "atom"},
                headers={"User-Agent": CONFIG.sec_user_agent},
            )
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception:  # noqa: BLE001
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(days=CONFIG.sec_lookback_days)
        items: list[NewsItem] = []
        for entry in root.iter(f"{_SEC_ATOM}entry"):
            title = (entry.findtext(f"{_SEC_ATOM}title") or "").strip()
            updated = (entry.findtext(f"{_SEC_ATOM}updated") or "").strip()
            if not title:
                continue
            # EDGAR's atom title is just the form type ("8-K - Current report"); the
            # filing date is the signal, so fold it into the headline. Lookback drops
            # stale filings (entries are newest-first).
            filed = ""
            try:
                when = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                if when < cutoff:
                    break       # everything after this is older -> stop
                filed = f" filed {when.date()}"
            except (ValueError, TypeError):
                pass
            items.append(NewsItem(
                title=f"SEC {form}{filed} ({title})", publisher="SEC EDGAR",
                published_utc=updated, sentiment="",
            ))
            if len(items) >= CONFIG.news_per_symbol:
                break
        return items

    def headlines(self, symbol: str) -> list[str]:
        """Prompt-ready headline strings for the Analyst."""
        return [i.as_prompt_line() for i in self.fetch(symbol)]

    @staticmethod
    def net_sentiment(items: list[NewsItem]) -> float:
        """Mean of signed per-article sentiment, -1..1 (0 when none labeled)."""
        signs = [_SENT_SIGN.get(i.sentiment, 0) for i in items if i.sentiment]
        return sum(signs) / len(signs) if signs else 0.0
