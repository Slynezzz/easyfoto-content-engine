"""Кто из пришедших в бот пришёл с какого ролика — сигнал для обучения.

Точное соответствие: метка с номером ролика (utm_vk_clip_p12, utm_yt_short_p12)
— пользователь засчитывается этому ролику.

По времени: ссылка из шапки профиля (utm_ig_bio и т.п.) номера ролика не несёт.
Такой пользователь делится поровну между роликами этой площадки, вышедшими
за WINDOW до его прихода — обычно человек переходит в профиль сразу после ролика.

Ценность пользователя: пришёл — 1, сделал первое фото — ещё 2, купил — ещё 6.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .db import DB

WINDOW = timedelta(hours=48)
VALUE = {"reg": 1.0, "photo": 2.0, "bought": 6.0}
POST_RE = re.compile(r"^p(\d+)$")


def _dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)  # Railway Postgres хранит время в UTC
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def publications(db: DB) -> dict[str, list[tuple[int, datetime]]]:
    """Когда ролики вышли на каждой площадке: {площадка: [(post_id, время)]}."""
    out: dict[str, list[tuple[int, datetime]]] = {"ig": [], "vk": [], "yt": []}
    for r in db.q("SELECT id, published_at FROM posts WHERE published_at IS NOT NULL"):
        if (t := _dt(r["published_at"])):
            out["ig"].append((r["id"], t))
    for r in db.q("SELECT post_id, platform, due_at FROM crossposts WHERE status IN ('published','sent')"):
        key = "vk" if r["platform"].startswith("vk") else r["platform"]
        if (t := _dt(r["due_at"])) and key in out:
            out[key].append((r["post_id"], t))
    return out


def attribute(users: list[dict], pubs: dict[str, list[tuple[int, datetime]]]) -> dict[int, dict]:
    """{post_id: {"regs", "photo", "bought", "points"}} — дробные, если пользователь поделён."""
    out: dict[int, dict] = {}

    def add(pid: int, u: dict, share: float) -> None:
        a = out.setdefault(pid, {"regs": 0.0, "photo": 0.0, "bought": 0.0, "points": 0.0})
        a["regs"] += share
        a["photo"] += share * u["photo"]
        a["bought"] += share * u["bought"]
        a["points"] += share * (VALUE["reg"] + VALUE["photo"] * u["photo"] + VALUE["bought"] * u["bought"])

    for u in users:
        m = POST_RE.match(u.get("campaign") or "")
        if m:
            add(int(m.group(1)), u, 1.0)
            continue
        when = _dt(u.get("registered_at"))
        cands = [pid for pid, t in pubs.get(u.get("source") or "", []) if when and when - WINDOW <= t <= when]
        for pid in cands:
            add(pid, u, 1.0 / len(cands))
    return out


def summary(conv: dict[int, dict]) -> str:
    if not conv:
        return ""
    regs = sum(a["regs"] for a in conv.values())
    photo = sum(a["photo"] for a in conv.values())
    bought = sum(a["bought"] for a in conv.values())
    top = sorted(conv.items(), key=lambda kv: -kv[1]["points"])[:3]
    best = ", ".join(f"#{pid} ({a['regs']:.1f} пришли, {a['bought']:.1f} купили)" for pid, a in top)
    return (f"Переходы в бот с роликов: {regs:.0f} пришли → {photo:.0f} сделали фото → {bought:.0f} купили. "
            f"Больше всех привели: {best}.")
