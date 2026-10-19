"""Поиск через Google Programmable Search Engine (Custom Search JSON API).

Нужен разведчику трендов, когда у Gemini нет встроенного поиска Google
(например, посредник принимает запросы только в формате OpenAI).

Важно: Google закрыл этот API для новых подключений и отключает его
1 января 2027 года; у уже подключённых проектов он работает до этой даты.
Бесплатно — 100 запросов в день; разведчику хватает ~6 в неделю.
"""
from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

CSE_URL = "https://www.googleapis.com/customsearch/v1"
UA = "Mozilla/5.0 (compatible; EasyfotoContentEngine/1.0)"


class SearchError(RuntimeError):
    pass


@dataclass
class Hit:
    title: str
    link: str
    snippet: str
    text: str = ""


def search(key: str, cx: str, query: str, num: int = 5, recent: str = "m1") -> list[Hit]:
    """Поиск. recent: d7 — неделя, m1 — месяц, m3 — три месяца."""
    try:
        r = requests.get(CSE_URL, params={"key": key, "cx": cx, "q": query, "num": num,
                                          "dateRestrict": recent, "safe": "active"}, timeout=30)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise SearchError(str(e).replace(key, "***")) from None
    if "error" in data:
        err = data["error"]
        raise SearchError(f"{err.get('code')}: {err.get('message')}")
    return [Hit(i.get("title", ""), i.get("link", ""), i.get("snippet", ""))
            for i in data.get("items") or [] if i.get("link")]


_DROP = re.compile(r"<(script|style|noscript|svg|nav|footer|header|form)\b.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def page_text(url: str, limit: int = 4000) -> str:
    """Основной текст страницы: без скриптов, меню и разметки. Пусто, если не вышло."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    except requests.RequestException:
        return ""
    if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html"):
        return ""
    r.encoding = r.encoding or r.apparent_encoding
    raw = _DROP.sub(" ", r.text)
    text = _WS.sub(" ", html.unescape(_TAG.sub(" ", raw))).strip()
    return text[:limit]


def gather(key: str, cx: str, queries: list[str], per_query: int = 4, read_pages: int = 6) -> list[Hit]:
    """Ищет по нескольким запросам, убирает повторы, читает первые страницы целиком."""
    hits: list[Hit] = []
    seen: set[str] = set()
    errors = []
    for q in queries:
        try:
            found = search(key, cx, q, num=per_query)
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
        h.text = page_text(h.link)
    return hits
