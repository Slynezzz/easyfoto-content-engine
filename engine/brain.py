"""Мозг экспертного аккаунта: пост с картинкой по теме — работой команды из трёх ролей.

1. Стратег (код): какой этап воронки сейчас нужен (охват / доверие / заявка — по балансу последних
   постов), какой сегмент давно не получал внимания, можно ли сегодня показать лицо автора,
   какие посты уже приводили заявки.
2. Копирайтер (нейросеть): инсайт сегмента, возражение, 5 хуков на разных механиках, лучший —
   заголовок на картинку; текст по правилам площадки; сцена для картинки.
3. Редактор-критик (нейросеть): проверяет черновик по чек-листу и переписывает слабое.
   Если критик не ответил — идёт черновик копирайтера.

Затем картинка: иллюстрация по смыслу или (иногда) автор по его фото «реф», и сборка
карточки 4:5 с заголовком (engine/postcard.py).
"""
from __future__ import annotations

import json
import logging
import secrets
from collections import Counter
from pathlib import Path

from . import brief, images, niche, platforms, postcard
from . import playbook as pbm
from .config import Config
from .db import DB
from .llm import AI, AIError

log = logging.getLogger(__name__)

STRATEGY_PATH = Path(__file__).with_name("strategy_expert.json")
AUTHOR_EVERY = 4          # лицо автора — не чаще одного поста из четырёх
HISTORY = 12              # по скольким последним постам считаем баланс воронки


class BrainError(RuntimeError):
    pass


def strategy() -> dict:
    return json.loads(STRATEGY_PATH.read_text(encoding="utf-8"))


def _recent(db: DB, n: int = HISTORY) -> list[dict]:
    out = []
    for r in db.q("SELECT id, script_json FROM posts WHERE format='post' AND status != 'rejected' "
                  "ORDER BY id DESC LIMIT ?", (n,)):
        try:
            b = json.loads(r["script_json"] or "{}").get("brain") or {}
        except ValueError:
            b = {}
        out.append({"id": r["id"], **b})
    return out


def pick_stage(recent: list[dict], strat: dict) -> str:
    """Этап, которого в последних постах меньше всего относительно целевой доли."""
    counts = Counter(r.get("stage") for r in recent)
    total = max(1, len(recent))
    return max(strat["stages"], key=lambda s: strat["stages"][s]["share"] - counts.get(s, 0) / total)


def pick_segment(recent: list[dict], audiences: dict) -> str | None:
    if not audiences:
        return None
    counts = Counter(r.get("segment") for r in recent[:8])
    return min(audiences, key=lambda a: (counts.get(a, 0), list(audiences).index(a)))


def author_allowed(db: DB, recent: list[dict]) -> bool:
    if not images.pick_refs(db):
        return False
    return sum(1 for r in recent[: AUTHOR_EVERY - 1] if r.get("image_mode") == "author") == 0


def lead_winners(db: DB, limit: int = 3) -> list[str]:
    """Хуки постов, которые уже приводили заявки в бот — что работает у этой аудитории."""
    rows = db.q("SELECT post_id, COUNT(*) came, SUM(CASE WHEN form_at IS NOT NULL THEN 1 ELSE 0 END) forms "
                "FROM leads WHERE post_id IS NOT NULL GROUP BY post_id ORDER BY forms DESC, came DESC LIMIT ?",
                (limit,))
    out = []
    for r in rows:
        p = db.post(r["post_id"])
        if not p:
            continue
        try:
            hook = json.loads(p["script_json"] or "{}").get("script", {}).get("hook", "")
        except ValueError:
            hook = ""
        if hook:
            out.append(f"«{hook.replace('*', '')}» — пришли {r['came']}, заявок {r['forms'] or 0}")
    return out


SYSTEM = """Ты — команда лучших специалистов по привлечению клиентов для эксперта: стратег, копирайтер,
специалист по психологии клиента и продажам. Вся команда работает ради одной цели — чтобы автор
получил новые заявки и заказы от своей целевой аудитории. Ты пишешь посты с картинкой для соцсетей.

Принципы:
- Говори о читателе и его бизнесе, а не об авторе и технологиях. Сначала ситуация и боль, потом решение.
- Одна мысль на пост. Конкретика вместо общих слов. Ценность в самом посте — его хочется сохранить.
- Хайп — только если он честный и полезен бизнесу читателя; никаких трагедий, политики и скандалов.
- Психология — только честная: узнавание, цена бездействия на реальном сценарии, любопытство, польза.
  Никаких манипуляций, ложной срочности, выдуманных отзывов, кейсов, цифр, цен и гарантий.

БРИФ АВТОРА (источник правды; ничего не выдумывай сверх него):
{brief}

Пиши по-русски, живо, коротко, от первого лица автора. Отвечай ТОЛЬКО JSON по схеме."""

WRITE_SCHEMA = """Схема ответа:
{{
  "segment": "id сегмента из списка",
  "insight": "ситуация и боль сегмента по этой теме — 1 фраза",
  "objection": "какое возражение снимает пост (или пусто)",
  "hooks": [{{"mechanic": "id механики", "text": "хук до 70 символов"}}, ... ровно 5 шт., механики разные],
  "best": номер лучшего хука от 0 до 4,
  "headline": "заголовок на картинку — лучший хук, до 60 символов; 1–2 ключевых слова в *звёздочках*",
  "subline": "подзаголовок на картинку до 70 символов — обещание пользы поста, без звёздочек",
  "body": "текст поста по правилам площадки: первая строка цепляет, дальше польза, в конце призыв этапа воронки{link_hint}",
  "hashtags": ["до {tags} тематических хэштегов с #"],
  "image": {{
    "mode": "illustration или author",
    "title": "смысл картинки в 3–6 словах",
    "where": "где происходит",
    "doing": "что происходит (для author — что делает автор)",
    "wearing": "во что одет автор (только для author)",
    "around": "детали вокруг",
    "light": "свет и стиль"
  }}
}}"""

CRITIC_SYSTEM = """Ты — редактор-критик в команде маркетинга эксперта. Твоя задача — чтобы пост привёл заявки.
Проверь черновик строго по чек-листу и перепиши всё слабое. Не добавляй фактов, которых нет в брифе,
теме и черновике. Сохрани смысл, этап воронки и правила площадки. Отвечай ТОЛЬКО JSON."""

CRITIC_SCHEMA = """Схема ответа:
{
  "scores": {"пункт чек-листа коротко": оценка от 1 до 5, ...},
  "fixes": ["что исправлено", ...],
  "headline": "финальный заголовок на картинку до 60 символов, 1–2 слова в *звёздочках*",
  "subline": "финальный подзаголовок до 70 символов",
  "body": "финальный текст поста",
  "hashtags": ["..."]
}"""


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def write_prompt(request: dict, stage: str, segment: str | None, author_ok: bool, strat: dict,
                 audiences: dict, winners: list[str], trends: list[str]) -> str:
    st = strat["stages"][stage]
    pl = platforms.get(request.get("platform")) or platforms.get("tg")
    lines = [
        f"ТЕМА (заказ владельца): {request.get('title', '')}",
        *( [f"Как подать: {request['angle']}"] if request.get("angle") else []),
        *( [f"Источник (не выдумывай сверх него): {request['url']}"] if request.get("url") else []),
        "",
        f"ЭТАП ВОРОНКИ: {st['title']} — {st['goal']}.",
        "Подходящие углы: " + "; ".join(st["angles"]),
        f"Призыв этапа: {st['cta']}.",
        "",
        "СЕГМЕНТЫ АУДИТОРИИ (выбери тот, кому тема нужнее всего):",
        *[f"  {k}: {v.get('about', '')}" for k, v in audiences.items()],
        *( [f"Предпочтительно: {segment} — давно не писали для него, если тема подходит."] if segment else []),
        "",
        "МЕХАНИКИ ХУКОВ:",
        *[f"  {k}: {v}" for k, v in strat["mechanics"].items()],
        "",
        "КАРТА ВОЗРАЖЕНИЙ: " + "; ".join(strat["objections"]),
        "",
        f"ПЛОЩАДКА: {pl['rules']}",
        "",
        "КАРТИНКА: " + ("можно показать автора (mode=author) — если пост личный: история, кейс, закулисье, "
                        "мнение; автор в реалистичной сцене, по пояс; иначе illustration."
                        if author_ok else "только illustration — без людей в кадре крупно, без текста и логотипов."),
        "Иллюстрация — один ясный образ смысла поста (предмет, ситуация, метафора процесса), "
        "который считывается за секунду; нижняя треть спокойная — туда ляжет заголовок.",
    ]
    if winners:
        lines += ["", "ЧТО УЖЕ ПРИВОДИЛО ЗАЯВКИ (учти механику и тон):", *[f"  — {w}" for w in winners]]
    if trends:
        lines += ["", "СВЕЖИЕ ТРЕНДЫ НИШИ (используй, если уместно):", *[f"  — {t}" for t in trends[:6]]]
    link_hint = ("; ссылку не пиши — она добавится автоматически" if pl.get("hashtags", 0) == 0 or
                 request.get("platform") in ("tg", "vk") else "")
    lines += ["", WRITE_SCHEMA.format(link_hint=link_hint, tags=pl["hashtags"])]
    return "\n".join(lines)


def critic_prompt(draft: dict, request: dict, stage: str, strat: dict) -> str:
    pl = platforms.get(request.get("platform")) or platforms.get("tg")
    return "\n".join([
        f"Тема: {request.get('title', '')}",
        f"Этап воронки: {strat['stages'][stage]['title']} — {strat['stages'][stage]['goal']}",
        f"Площадка: {pl['rules']}",
        "",
        "ЧЕК-ЛИСТ:", *[f"  {i + 1}. {c}" for i, c in enumerate(strat["critic"])],
        "",
        "ЧЕРНОВИК:",
        json.dumps({k: draft.get(k) for k in ("headline", "subline", "body", "hashtags", "objection", "insight")},
                   ensure_ascii=False, indent=1),
        "",
        CRITIC_SCHEMA,
    ])


def _ask(ai: AI, cfg: Config, system: str, prompt: str) -> dict:
    """Сильная модель текстов, при ошибке — основная."""
    if cfg.copy_model:
        try:
            return ai.json(system, prompt, model=cfg.copy_model)
        except AIError as e:
            log.warning("brain copy model: %s", e)
    return ai.json(system, prompt)


def write(cfg: Config, db: DB, ai: AI, request: dict) -> dict:
    """Текст поста и сцена картинки. Возвращает dict с полями поста и метаданными стратегии."""
    strat = strategy()
    pb = pbm.load(cfg.playbook_path)
    audiences = pb.get("audiences", {})
    recent = _recent(db)
    stage = request.get("stage") or pick_stage(recent, strat)
    segment = pick_segment(recent, audiences)
    author_ok = author_allowed(db, recent)
    system = SYSTEM.format(brief=brief.load(cfg) or "(бриф не задан)")
    prompt = write_prompt(request, stage, segment, author_ok, strat, audiences,
                          lead_winners(db), pb.get("trend_notes", []))
    try:
        draft = _ask(ai, cfg, system, prompt)
    except AIError as e:
        raise BrainError(f"копирайтер не ответил: {e}") from None
    if not draft.get("body") or not draft.get("headline"):
        raise BrainError("копирайтер вернул пустой пост")

    final, critic = dict(draft), {}
    try:
        critic = _ask(ai, cfg, CRITIC_SYSTEM + "\n\nБРИФ:\n" + (brief.load(cfg) or ""),
                      critic_prompt(draft, request, stage, strat))
        for k in ("headline", "subline", "body", "hashtags"):
            if critic.get(k):
                final[k] = critic[k]
    except AIError as e:
        log.warning("critic failed: %s", e)

    pl = platforms.get(request.get("platform")) or platforms.get("tg")
    image = dict(draft.get("image") or {})
    if image.get("mode") != "author" or not author_ok:
        image["mode"] = "illustration"
    tags = [t if t.startswith("#") else f"#{t}" for t in (final.get("hashtags") or []) if t][: pl["hashtags"]]
    hooks = [h for h in (draft.get("hooks") or []) if isinstance(h, dict)][:5]
    best = draft.get("best") if isinstance(draft.get("best"), int) and 0 <= draft.get("best") < len(hooks) else 0
    return {
        "mechanic": (hooks[best].get("mechanic") if hooks else "") or "x",
        "stage": stage,
        "segment": draft.get("segment") if draft.get("segment") in audiences else segment,
        "insight": _clip(draft.get("insight", ""), 300),
        "objection": _clip(draft.get("objection", ""), 200),
        "hooks": hooks,
        "headline": _clip(final.get("headline", ""), 70),
        "subline": _clip((final.get("subline") or "").replace("*", ""), 80),
        "body": _clip(final.get("body", ""), pl["caption_limit"] - 200),
        "hashtags": tags,
        "image": image,
        "critic": {"scores": critic.get("scores") or {}, "fixes": (critic.get("fixes") or [])[:6]},
    }


AUTHOR_SCENE_HINT = ("Реалистичная фотография, журнальный стиль. Человек — автор, его лицо как на фото-образцах. "
                     "Кадр по пояс, лицо в верхней трети. Никакого текста и логотипов в кадре.")


def picture(cfg: Config, db: DB, ai: AI, image: dict) -> tuple["object", str]:
    """Картинка к посту: иллюстрация по смыслу или автор по его фото «реф»."""
    images._spend_image_budget(cfg, db)
    if image.get("mode") == "author":
        refs = images.pick_refs(db)
        scene = {**image, "light": f"{image.get('light') or 'мягкий естественный свет'}. {AUTHOR_SCENE_HINT}"}
        return images._generate_image(cfg, ai, refs, scene)
    prompt = (postcard.ILLUSTRATION_PROMPT + "\nКартинка:\n"
              f"  Смысл: {image.get('title', '')}\n  Где: {image.get('where', '')}\n"
              f"  Что происходит: {image.get('doing', '')}\n  Детали: {image.get('around', '')}\n"
              f"  Свет и стиль: {image.get('light') or 'мягкий естественный свет, современная редакционная фотография'}")
    from io import BytesIO

    from PIL import Image
    res = ai.image(prompt, [])
    try:
        return Image.open(BytesIO(res.data)).convert("RGB"), res.provider
    except OSError:
        raise AIError("посредник вернул не картинку") from None


def compose_caption(post: dict) -> str:
    tags = " ".join(post["hashtags"])
    return post["body"].strip() + (f"\n\n{tags}" if tags else "")


def make_post(cfg: Config, db: DB, ai: AI, request: dict) -> int:
    """Пост с картинкой по теме для площадки. Сохраняет пост со статусом 'rendered'."""
    post = write(cfg, db, ai, request)
    try:
        im, provider = picture(cfg, db, ai, post["image"])
    except AIError as e:
        raise BrainError(f"картинка не получилась: {e}") from None
    token = secrets.token_urlsafe(18)
    out = cfg.media_dir / f"{token}.jpg"
    nic = niche.load(cfg.niche)
    postcard.compose(im, post["headline"], post["subline"], out,
                     brand=f"{nic.get('lead_author', '')} · AI-архитектор".strip(" ·"),
                     author_photo=post["image"].get("mode") == "author")
    script = {"script": {"hook": post["headline"]}, "post_caption": compose_caption(post), "llm_ok": True,
              "brain": {k: post[k] for k in ("stage", "segment", "insight", "objection", "hooks", "critic", "mechanic")}
              | {"image_mode": post["image"]["mode"], "image_provider": provider, "subline": post["subline"]}}
    return db.create_post(
        target=request.get("platform"), topic_id=request.get("topic_id"),
        status="rendered", format="post", hook_id=f"post_{post['mechanic']}",
        theme=f"stage_{post['stage']}", audience=post["segment"], explore=0,
        script_json=json.dumps(script, ensure_ascii=False), asset_ids_json=[],
        video_path=None, cover_path=str(out), caption=compose_caption(post), media_token=token,
    )
