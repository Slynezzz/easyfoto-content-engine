"""Производство одного ролика: выбор → фото → сценарий → видео → пост в базе."""
from __future__ import annotations

import dataclasses
import logging
import random
import secrets
from datetime import date

from . import brief, images, planner
from . import playbook as pbm
from .config import Config
from .db import DB
from .llm import AI, AIError
from .video.render import render
from .video.templates import FORMATS

log = logging.getLogger(__name__)

MIN_PHOTOS = 3


class ProduceError(RuntimeError):
    pass


def produce(cfg: Config, db: DB, ai: AI, pb: dict, today: date,
            rng: random.Random | None = None, scheduled_for: str | None = None) -> int:
    """Собирает ролик и сохраняет пост со статусом 'rendered'. Возвращает id поста."""
    rng = rng or random.Random()
    befores = db.assets("before")
    allowed = set(pb["formats"]) - (set() if befores else {"before_after"})
    choice = pbm.choose(pb, today, cfg.explore_share, rng, allowed_formats=allowed)
    n = int(pb["formats"][choice.format].get("photos", 5))

    photos: list = []
    plan: planner.Plan | None = None
    brief_text = brief.load(cfg)

    if cfg.image_source in ("library", "mixed"):
        if images.untagged_count(db):
            try:  # размечаем сразу, не дожидаясь фоновой задачи
                images.tag_library(cfg, db, ai, pb["themes"], limit=n * 2)
            except images.TaggingError as e:
                log.warning("разметка перед сборкой не удалась: %s", e)
                db.set("last_tag_error", str(e)[:300])
        rows, theme = images.pick_library(db, choice.theme, n, rng, pbm.theme_weights(pb, today))
        if len(rows) >= MIN_PHOTOS:
            choice = dataclasses.replace(choice, theme=theme)
            photos = rows
            notes = [f"{r['title'] or 'образ без описания'} — {r['description'] or ''}".strip(" —")
                     for r in rows]
            plan = planner.plan(ai, pb, choice, len(rows), photo_notes=notes,
                                gender=cfg.persona_gender, bot=cfg.bot_username, brief_text=brief_text)

    if not photos and cfg.image_source in ("generate", "mixed") and images.pick_refs(db):
        plan = planner.plan(ai, pb, choice, n, generate_scenes=True,
                            gender=cfg.persona_gender, bot=cfg.bot_username, brief_text=brief_text)
        ids = []
        for scene in plan.scenes:
            try:
                ids.append(images.generate(cfg, db, ai, scene, choice.theme))
            except AIError as e:
                log.warning("генерация сцены «%s» не удалась: %s", scene.get("title"), e)
        photos = [db.one("SELECT * FROM assets WHERE id=?", (i,)) for i in ids]
        # подписи должны соответствовать реально получившимся кадрам
        ok_titles = [p["title"] for p in photos]
        plan.script["captions"] = [planner._clip(t or "", planner.LIMITS["caption"]) for t in ok_titles]

    if len(photos) < MIN_PHOTOS or plan is None:
        raise ProduceError(
            f"не хватает фото для ролика: есть {len(photos)}, нужно минимум {MIN_PHOTOS}. "
            "Добавь фото в библиотеку, или включи генерацию (IMAGE_SOURCE=mixed) "
            "и положи свои референсы в assets/refs."
        )

    before = None
    if choice.format == "before_after":
        before = sorted(befores, key=lambda r: (r["used_count"], rng.random()))[0]

    paths = [p["path"] for p in photos]
    script = plan.to_script()
    script.style = choice.style
    fmt = FORMATS[choice.format]
    seed = rng.randrange(1 << 30)
    spec = fmt(before["path"], paths, script, music_seed=seed) if before else fmt(paths, script, music_seed=seed)

    db.set("last_ai_error", "" if plan.llm_ok else plan.llm_error or "нейросеть не ответила")
    token = secrets.token_urlsafe(18)
    video = cfg.media_dir / f"{token}.mp4"
    cover = cfg.media_dir / f"{token}.jpg"
    render(spec, video, cover_path=cover, preset="fast")

    asset_ids = [p["id"] for p in photos] + ([before["id"]] if before else [])
    db.mark_used(asset_ids)
    plan.choice = dataclasses.asdict(choice)
    return db.create_post(
        status="rendered", format=choice.format, hook_id=choice.hook_id, theme=choice.theme,
        style=choice.style, audience=choice.audience,
        explore=int(choice.explore), script_json=plan.to_json(), asset_ids_json=asset_ids,
        video_path=str(video), cover_path=str(cover), caption=plan.full_caption(),
        media_token=token, scheduled_for=scheduled_for,
    )
