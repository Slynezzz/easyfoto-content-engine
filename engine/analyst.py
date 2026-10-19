"""Аналитик: раз в неделю оценивает ролики и обновляет плейбук.

Оценка ролика — взвешенная сумма рангов среди всех роликов:
  40% просмотры (насколько ролик раздал алгоритм),
  25% удержание первых секунд (1 − доля пролистываний; сила хука),
  20% вовлечённость (сохранения + репосты + комментарии на просмотр),
  15% среднее время просмотра.
Ранги вместо сырых чисел — чтобы один вирусный ролик не перекосил всё.
Для каждого формата, хука и темы считается средняя оценка относительно
средней по аккаунту; веса в плейбуке плавно сдвигаются к ней.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from statistics import mean

from . import playbook as pbm
from .config import Config
from .db import DB, now_iso
from .llm import AI, AIError

log = logging.getLogger(__name__)

MIN_POSTS = 6           # меньше — выводы будут шумом
MIN_PER_ARM = 2         # вариант с одним роликом не трогаем
MIN_AGE_HOURS = 48      # свежие ролики ещё набирают просмотры
WEIGHTS = {"plays": 0.40, "hold": 0.25, "engagement": 0.20, "watch": 0.15}

INSIGHT_SYSTEM = """Ты аналитик коротких видео. По таблице роликов аккаунта нейрофотографа
сформулируй 3–6 коротких практических выводов для сценариста: какие хуки, форматы и темы
работают, что избегать, что попробовать. Только то, что следует из данных; если данных мало —
так и скажи. Каждый вывод — одно предложение до 140 символов.
Ответ JSON: {"insights": ["...", "..."], "summary": "2–3 предложения для владельца"}"""


def skip_scale(values: list) -> float:
    """Meta не уточняет единицы reels_skip_rate. Если все значения ≤ 1 —
    это доли, иначе проценты. Решаем по всей выборке, а не по одному числу."""
    known = [float(v) for v in values if v is not None]
    return 100.0 if known and max(known) <= 1 else 1.0


def skip_pct(v: float | None, scale: float = 1.0) -> float | None:
    return None if v is None else float(v) * scale


def _ranks(values: list[float | None]) -> list[float]:
    """Ранг 0..1 (1 — лучший); пропуски получают середину."""
    known = sorted(v for v in values if v is not None)
    if len(known) < 2:
        return [0.5] * len(values)
    out = []
    for v in values:
        if v is None:
            out.append(0.5)
            continue
        below = sum(1 for k in known if k < v)
        equal = sum(1 for k in known if k == v)
        out.append((below + (equal - 1) / 2) / (len(known) - 1))
    return out


def score_posts(rows: list[dict]) -> list[dict]:
    plays = [r.get("plays") for r in rows]
    scale = skip_scale([r.get("skip_rate") for r in rows])
    hold = [None if r.get("skip_rate") is None else 1 - skip_pct(r["skip_rate"], scale) / 100 for r in rows]
    eng = [None if not r.get("plays") else
           ((r.get("saves") or 0) + (r.get("shares") or 0) + (r.get("comments") or 0)) / r["plays"]
           for r in rows]
    watch = [r.get("avg_watch_ms") for r in rows]
    rp, rh, re_, rw = _ranks(plays), _ranks(hold), _ranks(eng), _ranks(watch)
    scored = []
    for i, r in enumerate(rows):
        s = (WEIGHTS["plays"] * rp[i] + WEIGHTS["hold"] * rh[i]
             + WEIGHTS["engagement"] * re_[i] + WEIGHTS["watch"] * rw[i])
        scored.append({**r, "score": round(s, 4), "engagement_rate": eng[i], "hold": hold[i]})
    return scored


def arm_scores(scored: list[dict]) -> dict[str, dict[str, float]]:
    avg = mean(r["score"] for r in scored) or 1e-6
    out: dict[str, dict[str, float]] = {}
    for dim, col in (("formats", "format"), ("hooks", "hook_id"), ("themes", "theme")):
        groups: dict[str, list[float]] = {}
        for r in scored:
            groups.setdefault(r[col], []).append(r["score"])
        out[dim] = {k: round(mean(v) / avg, 3) for k, v in groups.items() if len(v) >= MIN_PER_ARM}
    return out


def eligible_rows(db: DB) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=MIN_AGE_HOURS)).isoformat(timespec="seconds")
    return [dict(r) for r in db.latest_metrics() if (r["published_at"] or "") <= cutoff]


def _table(scored: list[dict], pb: dict) -> str:
    lines = ["id | формат | хук | тема | просмотры | удержание | вовлечённость | ср.время,с | оценка"]
    for r in sorted(scored, key=lambda x: -x["score"]):
        hook = pb["hooks"].get(r["hook_id"], {}).get("pattern", r["hook_id"])
        lines.append(" | ".join(str(x) for x in (
            r["post_id"], r["format"], hook, r["theme"], r.get("plays"),
            None if r["hold"] is None else f"{r['hold']:.0%}",
            None if r["engagement_rate"] is None else f"{r['engagement_rate']:.1%}",
            None if not r.get("avg_watch_ms") else round(r["avg_watch_ms"] / 1000, 1),
            r["score"],
        )))
    return "\n".join(lines)


def run(cfg: Config, db: DB, ai: AI | None) -> str:
    """Пересчитывает плейбук. Возвращает отчёт для владельца."""
    rows = eligible_rows(db)
    if len(rows) < MIN_POSTS:
        return (f"Для выводов пока мало данных: {len(rows)} роликов со статистикой старше 2 суток, "
                f"нужно минимум {MIN_POSTS}. Плейбук не менял.")
    pb = pbm.load(cfg.playbook_path)
    scored = score_posts(rows)
    scores = arm_scores(scored)

    insights, summary = pb.get("insights", []), ""
    if ai is not None:
        try:
            res = ai.json(INSIGHT_SYSTEM, _table(scored, pb), temperature=0.3)
            got = [str(x)[:160] for x in (res.get("insights") or []) if str(x).strip()]
            if got:
                insights = got
            summary = str(res.get("summary") or "")[:600]
        except (AIError, AttributeError) as e:
            log.warning("insights failed: %s", e)

    new = pbm.reweight(pb, scores, note=f"анализ {now_iso()} по {len(rows)} роликам", insights=insights)
    new["updated_at"] = now_iso()
    db.x("INSERT INTO playbook_history(created_at, playbook_json, note) VALUES (?,?,?)",
         (now_iso(), json.dumps(pb, ensure_ascii=False), "до анализа"))
    pbm.save(cfg.playbook_path, new)
    return report(pb, new, scored, summary)


def _changes(old: dict, new: dict, dim: str, label: dict[str, str]) -> list[str]:
    out = []
    for k, v in new[dim].items():
        was = old[dim][k]["weight"]
        if abs(v["weight"] - was) >= 0.02:
            arrow = "▲" if v["weight"] > was else "▼"
            out.append(f"  {arrow} {label.get(k, k)}: {was:g} → {v['weight']:g}")
    return out


def report(old: dict, new: dict, scored: list[dict], summary: str) -> str:
    best = max(scored, key=lambda r: r["score"])
    hooks = {k: v["pattern"][:40] for k, v in new["hooks"].items()}
    themes = {k: v["title"] for k, v in new["themes"].items()}
    parts = [f"📊 Анализ по {len(scored)} роликам."]
    if summary:
        parts.append(summary)
    parts.append(f"Лучший: #{best['post_id']} ({best.get('plays') or 0} просмотров).")
    changes = (_changes(old, new, "formats", {}) + _changes(old, new, "hooks", hooks)
               + _changes(old, new, "themes", themes))
    parts.append("Изменения весов:\n" + ("\n".join(changes) if changes else "  без заметных изменений"))
    if new.get("insights"):
        parts.append("Выводы для сценариста:\n" + "\n".join(f"  — {x}" for x in new["insights"]))
    return "\n\n".join(parts)


def stats_text(db: DB) -> str:
    rows = [dict(r) for r in db.latest_metrics()]
    if not rows:
        return "Статистики пока нет: она появится через несколько часов после первых публикаций."
    total = sum(r.get("plays") or 0 for r in rows)
    lines = [f"Роликов со статистикой: {len(rows)}, просмотров всего: {total}"]
    top = sorted(rows, key=lambda r: -(r.get("plays") or 0))[:5]
    scale = skip_scale([r.get("skip_rate") for r in rows])
    lines.append("Топ по просмотрам:")
    for r in top:
        skip = skip_pct(r.get("skip_rate"), scale)
        lines.append(f"  #{r['post_id']} · {r['format']} · {r['theme']}: {r.get('plays') or 0} просм., "
                     f"{r.get('saves') or 0} сохр., {r.get('shares') or 0} репост."
                     + (f", пролистали {skip:.0f}%" if skip is not None else ""))
    return "\n".join(lines)
