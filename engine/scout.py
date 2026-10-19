"""Разведчик трендов: раз в неделю смотрит, что сейчас работает в Reels и Shorts.

Gemini с поиском Google изучает свежие материалы о трендах коротких видео
и возвращает:
  - новые паттерны хуков — они попадают в плейбук как эксперименты;
  - заметки о трендах (монтаж, стиль текста, подача) — их читает сценарист;
  - 0–2 актуальные темы для фото, если есть сильный повод.

Дальше всё решают твои цифры: аналитик поднимает вес того, что сработало
на твоём аккаунте, а трендовые хуки и темы, которые за 4 недели не
показали результат, разведчик убирает. Так плейбук обновляется, но не
засоряется.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

from . import playbook as pbm
from .config import Config
from .db import DB, now_iso
from .llm import AI, AIError

log = logging.getLogger(__name__)

MAX_TREND_HOOKS = 12
MAX_TREND_THEMES = 4
TRIAL_DAYS = 28
KEEP_IF_WEIGHT = 0.8   # трендовый вариант с весом ниже после испытательного срока удаляется

SYSTEM = """Ты — исследователь трендов коротких вертикальных видео (Instagram Reels, YouTube Shorts,
TikTok, VK Клипы) для русскоязычной аудитории. Пользуйся поиском, опирайся на свежие материалы
за последние 4–6 недель: обзоры трендов, разборы вирусных роликов, блоги маркетологов.

Ниша аккаунта: нейрофотограф — автор показывает свои фото, созданные Telegram-ботом из обычных
селфи (сцены, образы, эпохи, путешествия, деловые портреты). Цель роликов — досмотры,
комментарии, сохранения и переходы в бот по ссылке в профиле.

Правила для хуков:
{rules}

Ответь ТОЛЬКО JSON без пояснений:
{{
  "hooks": [{{"pattern": "текст хука до 60 символов, от первого лица мужчины",
             "idea": "почему это сейчас работает, одно предложение",
             "formats": ["подходящие форматы из списка: {formats}"]}}],
  "trend_notes": ["практический приём: монтаж, подача, стиль текста на видео, длительность — одно предложение до 160 символов"],
  "themes": [{{"id": "латиница_через_подчёркивание", "title": "название по-русски",
              "scenes": ["4–5 сцен для фото в этой теме"], "why": "почему сейчас"}}],
  "summary": "2–3 предложения для владельца аккаунта: что изменилось в трендах"
}}
3–5 хуков, 3–6 заметок, 0–2 темы (только если есть явный актуальный повод).
Не повторяй хуки из списка уже существующих."""


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9_]+", "_", text.lower()).strip("_")
    return s[:24]


def _parse_dt(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def prune(pb: dict, now: datetime) -> list[str]:
    """Убирает трендовые хуки и темы, не оправдавшие себя за испытательный срок."""
    removed = []
    for dim, cap in (("hooks", MAX_TREND_HOOKS), ("themes", MAX_TREND_THEMES)):
        trend = {k: v for k, v in pb[dim].items() if v.get("source") == "trend"}
        for k, v in trend.items():
            added = _parse_dt(v.get("added_at"))
            if added and now - added > timedelta(days=TRIAL_DAYS) and v["weight"] < KEEP_IF_WEIGHT:
                del pb[dim][k]
                removed.append(k)
        trend = sorted(((k, v) for k, v in pb[dim].items() if v.get("source") == "trend"),
                       key=lambda kv: (kv[1]["weight"], kv[1].get("added_at") or ""))
        while len(trend) > cap:
            k, _ = trend.pop(0)
            del pb[dim][k]
            removed.append(k)
    return removed


def merge(pb: dict, res: dict, today: date, now: datetime) -> dict[str, list[str]]:
    """Добавляет найденное в плейбук. Возвращает, что именно добавлено."""
    added: dict[str, list[str]] = {"hooks": [], "themes": []}
    formats = set(pb["formats"])
    existing = {" ".join(v["pattern"].lower().split()) for v in pb["hooks"].values()}
    for i, h in enumerate(res.get("hooks") or []):
        if not isinstance(h, dict):
            continue
        pattern = " ".join(str(h.get("pattern") or "").replace("*", "").split())
        if not pattern or len(pattern) > 80 or pattern.lower() in existing:
            continue
        fmts = [f for f in (h.get("formats") or []) if f in formats] or sorted(formats)
        key = f"t{today:%y%m%d}_{i}"
        pb["hooks"][key] = {"weight": 1.0, "base_weight": 1.0, "formats": fmts, "pattern": pattern,
                            "idea": str(h.get("idea") or "")[:200], "source": "trend",
                            "added_at": now.isoformat(timespec="seconds")}
        existing.add(pattern.lower())
        added["hooks"].append(pattern)
    for t in (res.get("themes") or [])[:2]:
        if not isinstance(t, dict):
            continue
        key = _slug(str(t.get("id") or ""))
        scenes = [str(x)[:120] for x in (t.get("scenes") or []) if str(x).strip()][:6]
        if len(key) < 3 or key in pb["themes"] or len(scenes) < 3:
            continue
        pb["themes"][key] = {"weight": 0.8, "base_weight": 0.8, "title": str(t.get("title") or key)[:40],
                             "scenes": scenes, "source": "trend",
                             "added_at": now.isoformat(timespec="seconds")}
        added["themes"].append(pb["themes"][key]["title"])
    notes = [str(x).replace("*", "")[:180] for x in (res.get("trend_notes") or []) if str(x).strip()][:6]
    if notes:
        pb["trend_notes"] = notes
    return added


def run(cfg: Config, db: DB, ai: AI, today: date | None = None, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    today = today or now.date()
    pb = pbm.load(cfg.playbook_path)
    rules = "\n".join(f"- {r}" for r in pb.get("rules", []))
    known = "\n".join(f"- {v['pattern']}" for v in pb["hooks"].values())
    system = SYSTEM.format(rules=rules, formats=", ".join(pb["formats"]))
    prompt = (f"Сегодня {today:%d.%m.%Y}. Найди, что сейчас работает в коротких видео в нише "
              f"нейрофото и AI-фотосессий и в целом в Reels/Shorts.\n\nУже есть хуки:\n{known}")
    try:
        res, sources = ai.research(system, prompt)
    except AIError as e:
        return f"⚠️ Разведка трендов не удалась: {e}"
    if not isinstance(res, dict):
        return "⚠️ Разведка трендов вернула непонятный ответ, плейбук не менял."

    db.x("INSERT INTO playbook_history(created_at, playbook_json, note) VALUES (?,?,?)",
         (now_iso(), __import__("json").dumps(pb, ensure_ascii=False), "до разведки трендов"))
    removed = prune(pb, now)
    added = merge(pb, res, today, now)
    pb["trends_updated_at"] = now.isoformat(timespec="seconds")
    pbm.save(cfg.playbook_path, pb)
    db.set("scout_last", now.isoformat(timespec="seconds"))

    parts = ["🔎 Разведка трендов"]
    if res.get("summary"):
        parts.append(str(res["summary"])[:600])
    if added["hooks"]:
        parts.append("Новые хуки (пойдут в эксперименты):\n" + "\n".join(f"  + {h}" for h in added["hooks"]))
    if added["themes"]:
        parts.append("Новые темы: " + ", ".join(added["themes"]))
    if pb.get("trend_notes"):
        parts.append("Приёмы для сценариста:\n" + "\n".join(f"  — {n}" for n in pb["trend_notes"]))
    if removed:
        parts.append(f"Убрал не сработавшие трендовые варианты: {len(removed)}.")
    if sources:
        parts.append("Источники:\n" + "\n".join(f"  • {s['title']}" for s in sources[:6]))
    return "\n\n".join(parts)
