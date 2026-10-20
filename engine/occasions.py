"""Календарь поводов: праздники, профессиональные дни, инфоповоды.

Постоянные поводы — в occasions.json (дата «ММ-ДД» или правило: n-й день
недели месяца, последний день недели, смещение от православной Пасхи,
256-й день года). Разовые инфоповоды и мемы добавляет разведчик трендов
в плейбук (раздел events) с точной датой.

Повод «активен» за lead дней до даты и after дней после. Ролик к поводу
генерируется из твоих фото — новые кадры в тему, без ручной загрузки.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

CALENDAR_PATH = Path(__file__).with_name("occasions.json")


@dataclass
class Occasion:
    id: str
    title: str
    day: date                     # дата повода в текущем цикле
    lead: int = 2
    after: int = 0
    weight: float = 1.0
    max_reels: int = 1
    mood: str = "upbeat"
    scenes: list[str] = field(default_factory=list)
    note: str = ""
    profession: bool = False
    source: str = "calendar"      # calendar | trend

    @property
    def theme_id(self) -> str:
        return f"occ:{self.id}"

    def days_left(self, today: date) -> int:
        return (self.day - today).days


def orthodox_easter(year: int) -> date:
    """Православная Пасха (алгоритм Меёса для юлианского календаря + 13 дней)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    return date(year, month, day) + timedelta(days=13)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def resolve(entry: dict, year: int) -> date:
    """Дата повода в указанном году."""
    if "date" in entry:
        m, d = (int(x) for x in entry["date"].split("-"))
        return date(year, m, d)
    rule = entry["rule"]
    if rule.startswith("easter:"):
        return orthodox_easter(year) + timedelta(days=int(rule.split(":")[1]))
    if rule.startswith("nth:"):
        _, n, wd, month = rule.split(":")
        return _nth_weekday(year, int(month), int(wd), int(n))
    if rule.startswith("last:"):
        _, wd, month = rule.split(":")
        return _last_weekday(year, int(month), int(wd))
    if rule == "day256":
        return date(year, 1, 1) + timedelta(days=255)
    raise ValueError(f"неизвестное правило даты: {rule}")


def _calendar() -> list[dict]:
    return json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))


def _from_entry(e: dict, day: date, source: str = "calendar") -> Occasion:
    return Occasion(
        id=e["id"], title=e["title"], day=day, lead=int(e.get("lead", 2)), after=int(e.get("after", 0)),
        weight=float(e.get("weight", 1)), max_reels=int(e.get("max_reels", 1)), mood=e.get("mood", "upbeat"),
        scenes=list(e.get("scenes", [])), note=e.get("note", ""), profession=bool(e.get("profession")),
        source=source,
    )


def active(today: date, events: list[dict] | None = None) -> list[Occasion]:
    """Поводы, к которым сейчас уместен контент, — ближайшие и самые весомые первыми."""
    out: list[Occasion] = []
    for e in _calendar():
        # повод из прошлого/следующего года тоже может попасть в окно (Новый год)
        for year in (today.year - 1, today.year, today.year + 1):
            occ = _from_entry(e, resolve(e, year))
            if occ.day - timedelta(days=occ.lead) <= today <= occ.day + timedelta(days=occ.after):
                out.append(occ)
    for ev in events or []:
        try:
            day = date.fromisoformat(ev["date"])
        except (KeyError, ValueError, TypeError):
            continue
        occ = _from_entry({**ev, "lead": ev.get("lead", 3), "max_reels": ev.get("max_reels", 1)}, day, "trend")
        if day - timedelta(days=occ.lead) <= today <= day + timedelta(days=occ.after):
            out.append(occ)
    # ближе к дате — важнее; вес праздника поднимает приоритет
    out.sort(key=lambda o: (-(o.weight / (1 + max(0, o.days_left(today)) * 0.3)), o.days_left(today)))
    return out


def upcoming(today: date, days: int = 21, events: list[dict] | None = None) -> list[Occasion]:
    """Ближайшие поводы (для команды /occasions)."""
    found: dict[str, Occasion] = {}
    for e in _calendar():
        for year in (today.year, today.year + 1):
            occ = _from_entry(e, resolve(e, year))
            if 0 <= occ.days_left(today) <= days and occ.id not in found:
                found[occ.id] = occ
    for ev in events or []:
        try:
            day = date.fromisoformat(ev["date"])
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= (day - today).days <= days:
            found[ev["id"]] = _from_entry(ev, day, "trend")
    return sorted(found.values(), key=lambda o: o.day)


def find(query: str, today: date, events: list[dict] | None = None) -> Occasion | None:
    """Поиск повода по названию — для «/make новый год»."""
    q = query.lower().strip()
    if not q:
        return None
    for occ in upcoming(today, 366, events):
        if q in occ.title.lower() or q == occ.id:
            return occ
    return None
