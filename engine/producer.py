"""Производство одного ролика: выбор → фото → сценарий → видео → пост в базе."""
from __future__ import annotations

import copy
import dataclasses
import logging
import random
import secrets
from datetime import date, datetime

from . import brief, images, music, planner
from . import occasions as occ_mod
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


def timing_hint(day, publish) -> str:
    """Как говорить о дате повода в день публикации: заранее, сегодня или уже после."""
    if isinstance(publish, str):
        try:
            publish = date.fromisoformat(publish[:10])
        except ValueError:
            return ""
    if isinstance(publish, datetime):
        publish = publish.date()
    left = (day - publish).days
    if left > 1:
        return (f"Ролик выйдет {publish:%d.%m}, до повода {left} дн. Пиши как о предстоящем "
                "(«скоро», «успей подготовить фото заранее»), НЕ «уже сегодня».")
    if left == 1:
        return "Ролик выйдет накануне повода: «уже завтра», «успеешь сделать фото к празднику»."
    if left == 0:
        return "Ролик выйдет в день повода: можно «уже сегодня»."
    return f"Повод был {-left} дн. назад: подавай как «если не успел — ещё не поздно», без «сегодня»."


def produce(cfg: Config, db: DB, ai: AI, pb: dict, today: date,
            rng: random.Random | None = None, scheduled_for: str | None = None,
            occasion: occ_mod.Occasion | None = None) -> int:
    """Собирает ролик и сохраняет пост со статусом 'rendered'. Возвращает id поста.

    occasion — праздник или инфоповод: тогда кадры генерируются заново из твоих
    фото в тему повода, а хук и тексты пишутся под него.
    """
    rng = rng or random.Random()
    pb = copy.deepcopy(pb)
    befores = db.assets("before")
    allowed = set(pb["formats"]) - (set() if befores else {"before_after"})
    choice = pbm.choose(pb, today, cfg.explore_share, rng, allowed_formats=allowed)
    if occasion:
        pb["themes"][occasion.theme_id] = {
            "weight": 0, "title": occasion.title, "scenes": occasion.scenes,
            "occasion": f"{occasion.title}, {occasion.day:%d.%m}", "note": occasion.note,
            "profession": occasion.profession, "timing": timing_hint(occasion.day, scheduled_for or today),
        }
        choice = dataclasses.replace(choice, theme=occasion.theme_id,
                                     occasion=f"{occasion.title} ({occasion.day:%d.%m})")
    n = int(pb["formats"][choice.format].get("photos", 5))

    photos: list = []
    plan: planner.Plan | None = None
    brief_text = brief.load(cfg)
    has_refs = bool(images.pick_refs(db))
    want_new = bool(occasion) or cfg.image_source == "generate" or (
        cfg.image_source == "mixed" and rng.random() < cfg.generate_share)
    if occasion and not has_refs:
        raise ProduceError("для ролика к поводу нужны твои фото: пришли их в админ-бот с подписью «реф» или «до»")

    def from_library():
        nonlocal choice
        if images.untagged_count(db):
            try:  # размечаем сразу, не дожидаясь фоновой задачи
                images.tag_library(cfg, db, ai, pb["themes"], limit=n * 2)
            except images.TaggingError as e:
                log.warning("разметка перед сборкой не удалась: %s", e)
                db.set("last_tag_error", str(e)[:300])
        rows, theme = images.pick_library(db, choice.theme, n, rng, pbm.theme_weights(pb, today))
        if len(rows) < MIN_PHOTOS:
            return [], None
        choice = dataclasses.replace(choice, theme=theme)
        notes = [f"{r['title'] or 'образ без описания'} — {r['description'] or ''}".strip(" —") for r in rows]
        return rows, planner.plan(ai, pb, choice, len(rows), photo_notes=notes,
                                  gender=cfg.persona_gender, bot=cfg.bot_username, brief_text=brief_text)

    def from_generation():
        k = max(MIN_PHOTOS, min(n, cfg.generate_photos))
        gplan = planner.plan(ai, pb, choice, k, generate_scenes=True,
                             gender=cfg.persona_gender, bot=cfg.bot_username, brief_text=brief_text)
        ids, errors = [], []
        for scene in gplan.scenes:
            try:
                ids.append(images.generate(cfg, db, ai, scene, choice.theme))
            except AIError as e:
                errors.append(str(e))
                log.warning("генерация сцены «%s» не удалась: %s", scene.get("title"), e)
                if (len(errors) >= 2 and not ids) or "дневной лимит" in str(e):
                    break  # два отказа с самого начала или лимит — дальше не тратим токены
        rows = [db.one("SELECT * FROM assets WHERE id=?", (i,)) for i in ids]
        if errors:
            db.set("last_image_error", errors[-1][:300])
        # подписи должны соответствовать реально получившимся кадрам
        gplan.script["captions"] = [planner._clip(r["title"] or "", planner.LIMITS["caption"]) for r in rows]
        return rows, gplan

    order = [from_generation, from_library] if (want_new and has_refs) else [from_library]
    if cfg.image_source == "mixed" and has_refs and from_generation not in order:
        order.append(from_generation)
    if occasion:
        order = [from_generation]
    for step in order:
        try:
            photos, plan = step()
        except AIError as e:
            log.warning("%s не удалось: %s", step.__name__, e)
            photos, plan = [], None
        if len(photos) >= MIN_PHOTOS and plan is not None:
            break

    if len(photos) < MIN_PHOTOS or plan is None:
        reason = db.get("last_image_error") if (want_new or occasion) else ""
        raise ProduceError(
            f"не хватает фото для ролика: есть {len(photos)}, нужно минимум {MIN_PHOTOS}."
            + (f" Генерация новых кадров не удалась: {reason}" if reason else
               " Добавь фото в библиотеку или свои фото с подписью «реф» для генерации.")
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
    track = music.pick(cfg, occasion.mood if occasion else None, random.Random(seed))
    if track:
        spec.music = track

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
