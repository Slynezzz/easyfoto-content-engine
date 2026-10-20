"""Балансы и лимиты платных сервисов: чтобы пополнить заранее, а не узнать по сбою.

Что умеем спросить напрямую:
- ElevenLabs — остаток кредитов на месяц (/v1/user/subscription);
- Tavily — израсходованные и доступные запросы (/usage);
- посредники нейросетей (SaintStore, AI STAR и т.п.) — если их шлюз отдаёт баланс
  ключа в одном из распространённых форматов (New API / One API, OpenAI billing).
  Если не отдаёт — показываем это честно и следим за ошибками оплаты.

Плюс «реактивные» признаки: последние ошибки генерации картинок, текстов и музыки
с кодами 401/402/403/429 или словами про баланс и квоту.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from .config import Config
from .db import DB

log = logging.getLogger(__name__)

QUOTA_PER_USD = 500_000          # так New API / One API считают «квоту» по умолчанию
UNLIMITED_USD = 1_000_000        # шлюзы так помечают ключ без лимита
MONEY_TROUBLE = re.compile(r"\b(401|402|403|429)\b|insufficient|quota|balance|credit|billing|payment|"
                           r"недостаточно|баланс|лимит.*исчерпан|оплат", re.I)


@dataclass
class Balance:
    name: str
    text: str               # что показать
    warn: str = ""          # непусто — пора пополнить


def _root(url: str) -> str:
    url = (url or "").rstrip("/")
    return url[:-3] if url.endswith("/v1") else url


def _get(url: str, headers: dict, params: dict | None = None):
    r = requests.get(url, headers=headers, params=params, timeout=20)
    try:
        data = r.json()
    except ValueError:
        data = None
    return r.status_code, data


def elevenlabs(cfg: Config) -> Balance | None:
    if not cfg.music_api_key:
        return None
    if "elevenlabs.io" not in cfg.music_api_base_url:
        return Balance("Музыка", "посредник музыки баланс не сообщает — смотри в кабинете")
    try:
        code, d = _get(f"{cfg.music_api_base_url}/v1/user/subscription", {"xi-api-key": cfg.music_api_key})
    except requests.RequestException as e:
        return Balance("ElevenLabs", f"не ответил ({type(e).__name__})")
    if code != 200 or not isinstance(d, dict):
        hint = " — у ключа нет права «User: read», остаток смотри в кабинете" if code in (401, 403) else ""
        return Balance("ElevenLabs", f"баланс недоступен (код {code}){hint}")
    used, limit = int(d.get("character_count") or 0), int(d.get("character_limit") or 0)
    left = max(0, limit - used)
    tier = d.get("tier") or "?"
    text = f"тариф {tier}: осталось {left:,} из {limit:,} кредитов".replace(",", " ")
    reset = d.get("next_character_count_reset_unix")
    if reset:
        from datetime import datetime, timezone
        text += f", обновятся {datetime.fromtimestamp(int(reset), timezone.utc):%d.%m}"
    warn = f"ElevenLabs: осталось {left} кредитов ({round(100 * left / limit) if limit else 0}%)" \
        if limit and left < 0.2 * limit else ""
    return Balance("ElevenLabs (музыка)", text, warn)


def tavily(cfg: Config) -> Balance | None:
    if (cfg.search_provider or "").lower() != "tavily" or not cfg.search_api_key:
        return None
    try:
        code, d = _get("https://api.tavily.com/usage", {"Authorization": f"Bearer {cfg.search_api_key}"})
    except requests.RequestException as e:
        return Balance("Tavily", f"не ответил ({type(e).__name__})")
    if code != 200 or not isinstance(d, dict):
        return Balance("Tavily", f"баланс недоступен (код {code})")
    acc, key = d.get("account") or {}, d.get("key") or {}
    used, limit = acc.get("plan_usage"), acc.get("plan_limit")
    if limit is None:
        used, limit = key.get("usage"), key.get("limit")
    if not limit:
        return Balance("Tavily (поиск трендов)", f"израсходовано {used or 0} запросов, лимит не задан")
    left = max(0, int(limit) - int(used or 0))
    warn = f"Tavily: осталось {left} запросов поиска" if left < 0.2 * int(limit) else ""
    return Balance("Tavily (поиск трендов)",
                   f"тариф {acc.get('current_plan') or '?'}: осталось {left} из {limit} запросов", warn)


def gateway(name: str, base: str, key: str, warn_usd: float) -> Balance | None:
    """Баланс ключа у посредника, если его шлюз это сообщает."""
    if not key or not base:
        return None
    root, h = _root(base), {"Authorization": f"Bearer {key}"}
    try:
        # New API: остаток ключа
        code, d = _get(f"{root}/api/usage/token", h)
        data = d.get("data") if isinstance(d, dict) else None
        if code == 200 and isinstance(data, dict) and "total_available" in data:
            if data.get("unlimited_quota"):
                return Balance(name, "ключ без лимита — остаток аккаунта смотри в кабинете посредника")
            left = float(data["total_available"]) / QUOTA_PER_USD
            return _usd(name, left, warn_usd)
        # One API / формат OpenAI billing
        code, sub = _get(f"{root}/v1/dashboard/billing/subscription", h)
        if code == 200 and isinstance(sub, dict) and sub.get("hard_limit_usd") is not None:
            limit = float(sub["hard_limit_usd"])
            if limit >= UNLIMITED_USD:
                return Balance(name, "ключ без лимита — остаток аккаунта смотри в кабинете посредника")
            end = date.today() + timedelta(days=1)
            code, use = _get(f"{root}/v1/dashboard/billing/usage", h,
                             {"start_date": (end - timedelta(days=99)).isoformat(), "end_date": end.isoformat()})
            spent = float(use.get("total_usage") or 0) / 100 if code == 200 and isinstance(use, dict) else 0.0
            return _usd(name, limit - spent, warn_usd)
    except (requests.RequestException, ValueError, TypeError) as e:
        return Balance(name, f"не ответил ({type(e).__name__})")
    return Balance(name, "баланс через API не сообщает — смотри в кабинете; об ошибках оплаты предупрежу")


def _usd(name: str, left: float, warn_usd: float) -> Balance:
    warn = f"{name}: на ключе осталось ≈ ${left:.2f}" if left < warn_usd else ""
    return Balance(name, f"осталось ≈ ${left:.2f}", warn)


def recent_errors(db: DB) -> list[str]:
    """Последние ошибки, похожие на кончившиеся деньги или лимит."""
    out = []
    for key, label in (("last_image_error", "Картинки"), ("last_ai_error", "Тексты"),
                       ("last_copy_error", "Модель текстов"), ("last_music_error", "Музыка")):
        err = db.get(key) or ""
        if err and MONEY_TROUBLE.search(err):
            out.append(f"{label}: {err[:180]}")
    return out


def collect(cfg: Config, db: DB) -> list[Balance]:
    items = [
        gateway("SaintStore (тексты)" if "saint" in cfg.gemini_base_url else "Посредник Gemini",
                cfg.gemini_base_url, cfg.gemini_api_key, cfg.balance_warn_usd),
        gateway("AI STAR (картинки)" if "starimg" in cfg.openai_base_url else "Посредник OpenAI",
                cfg.openai_base_url, cfg.openai_api_key, cfg.balance_warn_usd),
        gateway("Посредник картинок", cfg.image_api_base_url, cfg.image_api_key, cfg.balance_warn_usd),
        elevenlabs(cfg),
        tavily(cfg),
    ]
    seen, out = set(), []
    for b in items:
        if b and b.name not in seen:
            seen.add(b.name)
            out.append(b)
    return out


def report(cfg: Config, db: DB) -> str:
    lines = ["Балансы и лимиты:"]
    for b in collect(cfg, db):
        lines.append(f"{'⚠️' if b.warn else '•'} {b.name}: {b.text}")
    errs = recent_errors(db)
    if errs:
        lines.append("\nПохоже на нехватку средств или лимит:")
        lines += [f"⚠️ {e}" for e in errs]
    lines.append("\nКаждый день проверяю сам и пишу, только когда пора пополнить.")
    return "\n".join(lines)


def warnings(cfg: Config, db: DB) -> list[str]:
    try:
        warns = [b.warn for b in collect(cfg, db) if b.warn]
    except Exception as e:  # noqa: BLE001 — проверка балансов не должна ронять расписание
        log.warning("balances: %s", e)
        warns = []
    return warns + recent_errors(db)
