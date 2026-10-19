"""Поиск в интернете для разведчика трендов. Несколько поставщиков на выбор.

Какой использовать, задаётся переменными Railway:
  SEARCH_PROVIDER = tavily | serper | brave | google_cse
  SEARCH_API_KEY  = ключ выбранного сервиса
  (для google_cse — GOOGLE_API_KEY и GOOGLE_CSE_CX)
  SEARCH_BASE_URL — если ключ куплен у посредника и у него свой адрес.

Ещё один вариант — perplexity: это модель, которая ищет сама и сразу
пишет выводы со ссылками; он обрабатывается в scout.py, а не здесь.

Разведчику хватает ~6 запросов в неделю — это укладывается в бесплатные
лимиты любого из сервисов.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; EasyfotoContentEngine/1.0)"
PROVIDERS = ("tavily", "serper", "brave", "google_cse")
DEFAULT_BASE = {
    "tavily": "https://api.tavily.com",
    "serper": "https://google.serper.dev",
    "brave": "https://api.search.brave.com",
    "google_cse": "https://www.googleapis.com",
}


class SearchError(RuntimeError):
    pass


@dataclass
class Hit:
    title: str
    link: str
    snippet: str
    text: str = ""


@dataclass
class Searcher:
    provider: str
    key: str
    base: str = ""
    cx: str = ""          # только для google_cse

    def __post_init__(self):
        self.provider = (self.provider or "").lower().strip()
        if self.provider not in PROVIDERS:
            raise SearchError(f"неизвестный SEARCH_PROVIDER «{self.provider}», доступны: {', '.join(PROVIDERS)}")
        if not self.key:
            raise SearchError(f"для {self.provider} нужен ключ (SEARCH_API_KEY)")
        self.base = (self.base or DEFAULT_BASE[self.provider]).rstrip("/")

    def _scrub(self, text: str) -> str:
        return str(text).replace(self.key, "***")

    def _json(self, method: str, url: str, **kw) -> dict:
        try:
            r = requests.request(method, url, timeout=40, **kw)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise SearchError(self._scrub(e)) from None
        if r.status_code != 200 or (isinstance(data, dict) and "error" in data and not data.get("results")):
            err = data.get("error") if isinstance(data, dict) else None
            msg = err.get("message") if isinstance(err, dict) else err or data.get("detail") or data.get("message")
            raise SearchError(self._scrub(f"HTTP {r.status_code}: {msg or str(data)[:200]}"))
        return data

    def search(self, query: str, num: int = 5) -> list[Hit]:
        """Свежие (за последний месяц) результаты по запросу."""
        if self.provider == "tavily":
            data = self._json("POST", f"{self.base}/search",
                              headers={"Authorization": f"Bearer {self.key}"},
                              json={"query": query, "max_results": num, "search_depth": "basic",
                                    "time_range": "month", "include_raw_content": True})
            return [Hit(i.get("title", ""), i.get("url", ""), i.get("content", ""),
                        (i.get("raw_content") or "")[:4000])
                    for i in data.get("results") or [] if i.get("url")]
        if self.provider == "serper":
            data = self._json("POST", f"{self.base}/search", headers={"X-API-KEY": self.key},
                              json={"q": query, "num": num, "tbs": "qdr:m"})
            return [Hit(i.get("title", ""), i.get("link", ""), i.get("snippet", ""))
                    for i in data.get("organic") or [] if i.get("link")]
        if self.provider == "brave":
            data = self._json("GET", f"{self.base}/res/v1/web/search",
                              headers={"X-Subscription-Token": self.key, "Accept": "application/json"},
                              params={"q": query, "count": num, "freshness": "pm"})
            return [Hit(i.get("title", ""), i.get("url", ""), _strip(i.get("description", "")))
                    for i in (data.get("web") or {}).get("results") or [] if i.get("url")]
        # google_cse
        data = self._json("GET", f"{self.base}/customsearch/v1",
                          params={"key": self.key, "cx": self.cx, "q": query, "num": num,
                                  "dateRestrict": "m1", "safe": "active"})
        return [Hit(i.get("title", ""), i.get("link", ""), i.get("snippet", ""))
                for i in data.get("items") or [] if i.get("link")]


def from_config(cfg) -> Searcher | None:
    """Поисковик по настройкам; None — если поиск не настроен."""
    provider = (getattr(cfg, "search_provider", "") or "").lower().strip()
    if provider in ("", "none"):
        if getattr(cfg, "google_api_key", "") and getattr(cfg, "google_cse_cx", ""):
            return Searcher("google_cse", cfg.google_api_key, cx=cfg.google_cse_cx)
        return None
    if provider == "perplexity":
        return None  # модель с поиском, см. scout.py
    if provider == "google_cse":
        return Searcher("google_cse", getattr(cfg, "search_api_key", "") or cfg.google_api_key,
                        base=getattr(cfg, "search_base_url", ""), cx=cfg.google_cse_cx)
    return Searcher(provider, getattr(cfg, "search_api_key", ""), base=getattr(cfg, "search_base_url", ""))


_DROP = re.compile(r"<(script|style|noscript|svg|nav|footer|header|form)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _strip(text: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()


def page_text(url: str, limit: int = 4000) -> str:
    """Основной текст страницы: без скриптов, меню и разметки. Пусто, если не вышло."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    except requests.RequestException:
        return ""
    if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html"):
        return ""
    r.encoding = r.encoding or r.apparent_encoding
    return _strip(_DROP.sub(" ", r.text))[:limit]


def gather(searcher: Searcher, queries: list[str], per_query: int = 4, read_pages: int = 6) -> list[Hit]:
    """Ищет по нескольким запросам, убирает повторы, дочитывает страницы, у которых нет текста."""
    hits: list[Hit] = []
    seen: set[str] = set()
    errors = []
    for q in queries:
        try:
            found = searcher.search(q, num=per_query)
        except SearchError as e:
            errors.append(str(e))
            continue
        for h in found:
            if h.link not in seen:
                seen.add(h.link)
                hits.append(h)
    if not hits and errors:
        raise SearchError(errors[0])
    for h in hits[:read_pages]:
        if not h.text:
            h.text = page_text(h.link)
    return hits
