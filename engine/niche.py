"""Ниша аккаунта: про кого и для чего движок делает контент.

Один и тот же движок обслуживает разные аккаунты — свой экземпляр (сервис
в Railway) на каждый. Ниша задаётся переменной NICHE:
- neurophoto — продвижение бота-нейрофотографа Easyfotobot (по умолчанию);
- expert — личный бренд эксперта: контент про автора и его нишу, цель — заказчики.

Каждая ниша — файл engine/niches/<id>.json: формулировки для сценариста,
разведчика трендов и аналитика, стартовые плейбук и бриф, список поводов.
Подробности о продукте и аудитории — в брифе, его можно заменить из админ-бота.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).with_name("niches")
ENGINE = Path(__file__).parent
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь",
          "октябрь", "ноябрь", "декабрь"]


class NicheError(ValueError):
    pass


def available() -> list[str]:
    return sorted(p.stem for p in DIR.glob("*.json"))


@lru_cache(maxsize=8)
def load(niche_id: str = "neurophoto") -> dict:
    p = DIR / f"{niche_id or 'neurophoto'}.json"
    if not p.exists():
        raise NicheError(f"неизвестная ниша «{niche_id}». Есть: {', '.join(available())}")
    return json.loads(p.read_text(encoding="utf-8"))


def playbook_path(n: dict) -> Path:
    return ENGINE / n["playbook"]


def brief_path(n: dict) -> Path:
    return ENGINE / "prompts" / n["brief"]


def queries(n: dict, today: date) -> list[str]:
    ctx = {"month": MONTHS[today.month - 1], "year": today.year, "month_en": f"{today:%B}"}
    return [q.format(**ctx) for q in n["scout_queries"]]


def allows_occasion(n: dict, occasion_id: str) -> bool:
    """Календарные поводы: у ниши может быть свой список; инфоповоды от разведчика проходят всегда."""
    allowed = n.get("occasions")
    return allowed is None or occasion_id in allowed or occasion_id.startswith("ev_")


def activate(cfg) -> dict:
    """Подставляет стартовые плейбук и бриф ниши. Вызывается при запуске движка."""
    from . import brief, playbook
    n = load(cfg.niche)
    playbook.DEFAULT_PATH = playbook_path(n)
    brief.DEFAULT = brief_path(n)
    return n
