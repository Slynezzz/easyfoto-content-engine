"""Воронка: сколько людей пришло в Easyfotobot по ссылкам из роликов и что они сделали.

Бот уже умеет метки: ссылка t.me/<бот>?start=utm_<источник>_<канал>_<кампания>
записывает первый источник пользователя в таблицу user_utm. Здесь мы только
читаем базу бота (сессия в режиме READ ONLY) и считаем: пришли → сделали
первое фото → купили.

Подключение: в Railway в сервисе движка BOT_DATABASE_URL = ссылка на базу
нейрофотографа (можно сослаться на переменную DATABASE_URL его сервиса).
"""
from __future__ import annotations

import logging
import re

from .config import Config

log = logging.getLogger(__name__)

# метки для шапок профилей; у каждой площадки своя, чтобы видеть, откуда пришли
SOURCES = {"ig": "Instagram", "vk": "VK", "yt": "YouTube", "tg": "Telegram", "tt": "TikTok"}


class FunnelError(RuntimeError):
    pass


def start_param(source: str, medium: str = "bio", campaign: str = "") -> str:
    """Метка в формате бота: utm_<source>_<medium>_<campaign>. Telegram: до 64 символов, A-Za-z0-9_-."""
    parts = ["utm", source, medium] + ([campaign] if campaign else [])
    param = "_".join(re.sub(r"[^A-Za-z0-9-]", "", p) or "x" for p in parts)
    return param[:64]


def link(cfg: Config, source: str, medium: str = "bio", campaign: str = "") -> str:
    return f"https://t.me/{cfg.bot_username.lstrip('@')}?start={start_param(source, medium, campaign)}"


def links_text(cfg: Config) -> str:
    rows = [f"• {name}: {link(cfg, key)}" for key, name in SOURCES.items()]
    return ("Ссылки на бот с меткой площадки — поставь каждую в шапку своего профиля. "
            "Тогда /funnel покажет, сколько людей пришло с каждой площадки, сколько сделали фото и купили.\n\n"
            + "\n".join(rows))


def _connect(cfg: Config):
    if not cfg.bot_database_url:
        raise FunnelError("не подключена база бота: добавь в Railway BOT_DATABASE_URL")
    try:
        import psycopg2
    except ImportError as e:  # pragma: no cover — зависимость есть в requirements
        raise FunnelError("нет драйвера PostgreSQL (psycopg2)") from e
    try:
        conn = psycopg2.connect(cfg.bot_database_url, connect_timeout=15,
                                options="-c default_transaction_read_only=on -c statement_timeout=20000")
    except psycopg2.Error as e:
        raise FunnelError(f"база бота недоступна: {str(e).splitlines()[0][:200]}") from None
    conn.set_session(readonly=True, autocommit=True)
    return conn


BY_SOURCE = """
SELECT COALESCE(u.utm_source, '?'), COALESCE(u.utm_medium, ''), COALESCE(u.utm_campaign, ''),
       COUNT(*),
       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM generated_photos g WHERE g.user_id = u.user_id)),
       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM package_purchases p WHERE p.user_id = u.user_id)),
       COALESCE(SUM((SELECT SUM(p.amount_rub) FROM package_purchases p WHERE p.user_id = u.user_id)), 0)
FROM user_utm u
WHERE u.registered_at >= NOW() - %s * INTERVAL '1 day'
GROUP BY 1, 2, 3
ORDER BY 4 DESC
LIMIT 25
"""

TOTAL = """
SELECT COUNT(*),
       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM generated_photos g WHERE g.user_id = d.user_id)),
       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM package_purchases p WHERE p.user_id = d.user_id))
FROM user_data d
WHERE d.first_seen >= NOW() - %s * INTERVAL '1 day'
"""


def stats(cfg: Config, days: int = 30) -> dict:
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(TOTAL, (days,))
            total = cur.fetchone()
            cur.execute(BY_SOURCE, (days,))
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 — любая ошибка БД превращается в понятное сообщение
        raise FunnelError(f"запрос к базе бота не удался: {str(e).splitlines()[0][:200]}") from None
    finally:
        conn.close()
    return {"days": days, "total": {"users": total[0], "photo": total[1], "buyers": total[2]},
            "sources": [{"source": r[0], "medium": r[1], "campaign": r[2], "users": r[3], "photo": r[4],
                         "buyers": r[5], "rub": float(r[6] or 0)} for r in rows]}


def _pct(a: int, b: int) -> str:
    return f"{round(100 * a / b)}%" if b else "—"


def report(cfg: Config, days: int = 30) -> str:
    try:
        st = stats(cfg, days)
    except FunnelError as e:
        return f"Воронка недоступна: {e}"
    t = st["total"]
    lines = [f"Воронка за {days} дн.",
             f"Всего новых пользователей: {t['users']} → сделали фото {t['photo']} ({_pct(t['photo'], t['users'])})"
             f" → купили {t['buyers']} ({_pct(t['buyers'], t['users'])})"]
    if st["sources"]:
        lines.append("\nПо меткам (переход → фото → покупка):")
        for s in st["sources"]:
            name = SOURCES.get(s["source"], s["source"])
            tag = " ".join(x for x in (s["medium"], s["campaign"]) if x)
            rub = f", {s['rub']:.0f} ₽" if s["rub"] else ""
            lines.append(f"• {name} {tag}: {s['users']} → {s['photo']} → {s['buyers']}{rub}")
    else:
        lines.append("\nПереходов по меткам пока нет. Поставь ссылки с меткой в шапки профилей: /links")
    return "\n".join(lines)
