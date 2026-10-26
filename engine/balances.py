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


def probe_text_key(base: str, key: str, model: str) -> tuple[str, bool]:
    """Пробный запрос на 1 токен: жив ли ключ и не кончился ли лимит. (текст, исправен)."""
    from .llm import KEY_TROUBLE, MODEL_TROUBLE
    try:
        r = requests.post(f"{_root(base)}/v1/chat/completions", timeout=40,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": model, "max_tokens": 1,
                                "messages": [{"role": "user", "content": "ok"}]})
    except requests.RequestException as e:
        return f"не ответил ({type(e).__name__})", True
    if r.status_code == 200:
        return "✅ работает", True
    try:
        err = r.json().get("error")
        msg = err.get("message") if isinstance(err, dict) else str(err or r.text)
    except ValueError:
        msg = r.text
    msg = f"HTTP {r.status_code}: {str(msg)[:140]}"
    if KEY_TROUBLE.search(msg):
        return f"❌ исчерпан или отключён ({msg})", False
    if MODEL_TROUBLE.search(msg):
        return f"✅ ключ принят (модель {model} недоступна: {msg})", True
    return f"⚠️ {msg}", True


def probe_image_key(base: str, key: str) -> tuple[str, bool]:
    """Для ключей картинок дёшево проверить можно только, принят ли ключ."""
    try:
        r = requests.get(f"{_root(base)}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    except requests.RequestException as e:
        return f"не ответил ({type(e).__name__})", True
    if r.status_code in (401, 403):
        return f"❌ ключ не принят (HTTP {r.status_code})", False
    return "✅ ключ принят (остаток без генерации не проверить — при исчерпании переключусь сам)", True


def key_lines(cfg: Config, ai) -> tuple[list[str], list[str]]:
    """Строки по каждому ключу каждого посредника и предупреждения."""
    from datetime import datetime
    from .llm import KeyPool
    lines, warns = [], []
    groups = (
        ("gemini", "SaintStore" if "saint" in cfg.gemini_base_url else "Gemini-посредник", cfg.gemini_base_url, "text"),
        ("openai", "AI STAR" if "starimg" in cfg.openai_base_url else "OpenAI-посредник", cfg.openai_base_url, "image"),
        ("image", "Посредник картинок", cfg.image_api_base_url, "image"),
    )
    for name, label, base, kind in groups:
        pool = ai.pools[name] if ai is not None else None
        keys = pool.keys if pool else [k for k in [getattr(cfg, f"{name}_api_key", "")] if k]
        if not keys or not base:
            continue
        current = getattr(cfg, f"{name}_api_key", "")
        ok_count = 0
        lines.append(f"{label} — ключей: {len(keys)}")
        for i, key in enumerate(keys, 1):
            if kind == "text":
                text, ok = probe_text_key(base, key, cfg.gemini_text_model)
            else:
                text, ok = probe_image_key(base, key)
            if pool and not pool.usable(key) and ok:
                text += f"; отложен до {datetime.fromtimestamp(pool.bad_until(key)):%d.%m %H:%M} после ошибки"
            ok_count += ok
            mark = " ← сейчас" if key == current else ""
            bal = gateway("", base, key, cfg.balance_warn_usd)
            extra = f"; {bal.text}" if bal and "≈ $" in bal.text else ""
            lines.append(f"  {i}. {KeyPool.tail(key)}: {text}{extra}{mark}")
        if ok_count == 0:
            warns.append(f"{label}: ни один ключ не работает — генерация остановится")
        elif ok_count == 1 and len(keys) > 1:
            warns.append(f"{label}: остался один рабочий ключ из {len(keys)} — пора купить запасной")
    return lines, warns


def collect(cfg: Config, db: DB) -> list[Balance]:
    items = [
        elevenlabs(cfg),
        tavily(cfg),
    ]
    seen, out = set(), []
    for b in items:
        if b and b.name not in seen:
            seen.add(b.name)
            out.append(b)
    return out


def report(cfg: Config, db: DB, ai=None) -> str:
    lines = ["Ключи нейросетей:"]
    klines, kwarns = key_lines(cfg, ai)
    lines += klines or ["  ключи не заданы"]
    lines.append("\nДругие сервисы:")
    for b in collect(cfg, db):
        lines.append(f"{'⚠️' if b.warn else '•'} {b.name}: {b.text}")
    errs = recent_errors(db)
    if errs or kwarns:
        lines.append("\nВнимание:")
        lines += [f"⚠️ {e}" for e in kwarns + errs]
    lines.append("\nКаждый день проверяю сам и пишу, только когда пора пополнить. "
                 "Запасные ключи — в GEMINI_API_KEYS / OPENAI_API_KEYS / IMAGE_API_KEYS через запятую.")
    return "\n".join(lines)


def warnings(cfg: Config, db: DB, ai=None) -> list[str]:
    try:
        warns = [b.warn for b in collect(cfg, db) if b.warn] + key_lines(cfg, ai)[1]
    except Exception as e:  # noqa: BLE001 — проверка балансов не должна ронять расписание
        log.warning("balances: %s", e)
        warns = []
    return warns + recent_errors(db)
