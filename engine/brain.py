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
import re
import secrets
from collections import Counter
from pathlib import Path

from . import brief, images, keywords, niche, platforms, postcard, research
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
    for r in db.q("SELECT id, script_json FROM posts WHERE format IN ('post', 'carousel', 'dialog') AND status != 'rejected' "
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
  "headline": "заголовок на картинку — лучший хук, до 60 символов, одна фраза БЕЗ кавычек и тире; 1–2 ключевых слова в *звёздочках*",
  "subline": "подзаголовок до 70 символов — что получит читатель (польза, результат), а не описание поста: без «разбираю», «рассказываю», «в этом посте»; без звёздочек",
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

CAROUSEL_SCHEMA = """
Это КАРУСЕЛЬ: обложка (заголовок + картинка) и слайды. Добавь в ответ:
  "slides": [{{"title": "мысль слайда до 45 символов, 1 слово в *звёздочках*",
              "text": "раскрытие до 230 символов: факт с цифрой, шаг или пример — одна мысль",
              "source": "источник цифры на слайде (если есть цифра), иначе пусто"}}, ... от 4 до 6 слайдов],
  "cta_slide": {{"title": "призыв до 45 символов", "text": "до 160 символов — по правилам этапа и площадки"}}
Слайды идут логикой: ответ на заголовок → факты → что делать → вывод. Каждый слайд понятен сам по себе.
Поле "body" для карусели — подпись (объём — в требованиях выше): суть, зачем листать, призыв; детали — на слайдах."""

DIALOG_SCHEMA = """
Это ПОСТ С ПЕРЕПИСКОЙ: картинкой будет макет чата клиента с ботом — наглядный пример сценария
(не реальный диалог). Добавь в ответ:
  "dialog": {"bot_name": "кто отвечает, без реальных брендов, например «Агентство · помощник»",
             "time": "время первого сообщения клиента, например 23:40",
             "messages": [{"from": "client" или "bot", "text": "до 140 символов"}, ... от 5 до 7 сообщений]}
Сценарий показывает решение боли из поста: клиент пишет в неудобное время или с типовым вопросом → бот
отвечает по делу, уточняет, записывает или квалифицирует, передаёт человеку. Без цен, реальных компаний
и имён клиентов. Заголовок — про ситуацию в чате. Поле "body" — разбор сценария: что бот сделал на каждом
шаге и почему это важно для бизнеса (с фактами из исследования); "image" не нужен."""

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
  "hashtags": ["..."],
  "slides": [...] и "cta_slide": {...} — только если они есть в черновике (карусель): те же поля, исправленные
}"""


QUOTES = str.maketrans("", "", "«»\"„“”")
META_LEAD = re.compile(r"^\s*(разбираю|разберу|рассказываю|расскажу|показываю|покажу|объясняю|объясню|"
                       r"в этом посте|в посте|пост о том|сегодня)[^:—.]*[:—.]\s*", re.I)


def clean_headline(s: str) -> str:
    """Заголовок на картинке: без кавычек и тире — одна чистая фраза."""
    s = (s or "").translate(QUOTES)
    s = re.sub(r"\s+[—–-]\s+", ", ", s)
    s = re.sub(r"\s+", " ", s).strip(" ,—–-")
    return s[:1].upper() + s[1:]


def clean_subline(s: str) -> str:
    """Подзаголовок — польза для читателя, без «Разбираю возражение:» и звёздочек."""
    s = META_LEAD.sub("", (s or "").replace("*", "").translate(QUOTES)).strip()
    return s[:1].upper() + s[1:]


def platform_cta(strat: dict, platform: str | None) -> str:
    """Призыв, который оставляет читателя на той же площадке, где он увидел пост."""
    return strat.get("platform_cta", {}).get(platform or "", "вопрос в комментариях")


def length_for(strat: dict, platform: str | None, kind: str = "post") -> str:
    """Объём текста под площадку и формат: в Telegram пост с картинкой — это подпись (до 1024 знаков)."""
    rule = strat.get("length", {}).get(platform or "tg", {})
    if isinstance(rule, str):
        return rule
    return rule.get(kind) or rule.get("post") or "800–1500 символов"


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def write_prompt(request: dict, stage: str, segment: str | None, author_ok: bool, strat: dict,
                 audiences: dict, winners: list[str], trends: list[str], facts: str = "") -> str:
    st = strat["stages"][stage]
    pl = platforms.get(request.get("platform")) or platforms.get("tg")
    length = length_for(strat, request.get("platform"), request.get("kind", "post"))
    lines = [
        f"ТЕМА (заказ владельца): {request.get('title', '')}",
        "Заголовок и текст — строго про эту тему, её ключевые слова — в заголовке или первой строке. "
        "Подменять тему другой (даже близкой) нельзя.",
        *( [f"Как подать: {request['angle']}"] if request.get("angle") else []),
        *( [f"Источник (не выдумывай сверх него): {request['url']}"] if request.get("url") else []),
        "",
        facts or research.facts_block({}),
        "",
        "ТРЕБОВАНИЯ К ТЕКСТУ (пост должен быть «мясом» — причиной читать, сохранять, пересылать):",
        "- Заголовок — обещание или вопрос; текст ОБЯЗАН прямо и конкретно ответить на него в первых двух абзацах.",
        "- Фактура: минимум 3 факта с цифрами из исследования, с названием источника в тексте; цифр вне списка — ни одной.",
        "- Структура: цепляющая первая строка → прямой ответ / главный тезис → аргументы с фактами (можно списком) → "
        "практическая часть: что конкретно сделать, пример сценария из жизни сегмента → короткий вывод → призыв этапа.",
        f"- Объём текста: {length}. Каждое предложение несёт информацию: без воды, общих слов и цепочек риторических вопросов.",
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
        f"ПРИЗЫВ НА ЭТОЙ ПЛОЩАДКЕ: {platform_cta(strat, request.get('platform'))}",
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


def critic_prompt(draft: dict, request: dict, stage: str, strat: dict, facts: str = "") -> str:
    pl = platforms.get(request.get("platform")) or platforms.get("tg")
    length = length_for(strat, request.get("platform"), request.get("kind", "post"))
    return "\n".join([
        f"Тема: {request.get('title', '')}",
        f"Объём текста: {length}",
        facts or research.facts_block({}),
        "Если в тексте есть цифра, которой нет в фактах, — убери её или замени фактом из списка.",
        f"Этап воронки: {strat['stages'][stage]['title']} — {strat['stages'][stage]['goal']}",
        f"Площадка: {pl['rules']}",
        f"ПРИЗЫВ НА ЭТОЙ ПЛОЩАДКЕ: {platform_cta(strat, request.get('platform'))}",
        "",
        "ЧЕК-ЛИСТ:", *[f"  {i + 1}. {c}" for i, c in enumerate(strat["critic"])],
        "",
        "ЧЕРНОВИК:",
        json.dumps({k: draft.get(k) for k in ("headline", "subline", "body", "hashtags", "objection", "insight",
                                              "slides", "cta_slide", "dialog") if draft.get(k) is not None},
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


def write(cfg: Config, db: DB, ai: AI, request: dict, kind: str = "post") -> dict:
    """Текст поста и сцена картинки. Возвращает dict с полями поста и метаданными стратегии."""
    request = {**request, "kind": kind}
    strat = strategy()
    pb = pbm.load(cfg.playbook_path)
    audiences = pb.get("audiences", {})
    recent = _recent(db)
    stage = request.get("stage") or pick_stage(recent, strat)
    segment = pick_segment(recent, audiences)
    author_ok = author_allowed(db, recent)
    system = SYSTEM.format(brief=brief.load(cfg) or "(бриф не задан)")
    found = research.run(cfg, ai, request)
    facts = research.facts_block(found)
    prompt = write_prompt(request, stage, segment, author_ok, strat, audiences,
                          lead_winners(db), pb.get("trend_notes", []), facts)
    if request.get("platform") == "ig" and keywords.prompt_hint(db):
        prompt += "\n\n" + keywords.prompt_hint(db)
    if kind == "carousel":
        prompt += "\n" + CAROUSEL_SCHEMA.replace("{{", "{").replace("}}", "}")
    elif kind == "dialog":
        prompt += "\n" + DIALOG_SCHEMA
    try:
        draft = _ask(ai, cfg, system, prompt)
    except AIError as e:
        raise BrainError(f"копирайтер не ответил: {e}") from None
    if not draft.get("body") or not draft.get("headline"):
        raise BrainError("копирайтер вернул пустой пост")

    final, critic = dict(draft), {}
    try:
        critic = _ask(ai, cfg, CRITIC_SYSTEM + "\n\nБРИФ:\n" + (brief.load(cfg) or ""),
                      critic_prompt(draft, request, stage, strat, facts))
        for k in ("headline", "subline", "body", "hashtags", "slides", "cta_slide", "dialog"):
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
        "headline": _clip(clean_headline(final.get("headline", "")), 70),
        "subline": _clip(clean_subline(final.get("subline") or ""), 80),
        "body": _clip(final.get("body", ""), pl["caption_limit"] - 200),
        "hashtags": tags,
        "image": image,
        "critic": {"scores": critic.get("scores") or {}, "fixes": (critic.get("fixes") or [])[:6]},
        "research": {"method": found.get("method", ""), "error": found.get("error", ""),
                     "facts": found.get("facts", [])},
        "sources": used_sources(final.get("body", "") + " " + " ".join(
            str(x.get("source", "")) + " " + str(x.get("text", "")) for x in (final.get("slides") or [])
            if isinstance(x, dict)), found.get("facts", [])),
        "slides": [{"title": _clip(clean_headline(x.get("title", "")), 60), "text": _clip(x.get("text", ""), 260),
                    "source": _clip(x.get("source", ""), 70)}
                   for x in (final.get("slides") or []) if isinstance(x, dict) and x.get("title")][:6],
        "cta_slide": final.get("cta_slide") if isinstance(final.get("cta_slide"), dict) else None,
        "dialog": clean_dialog(final.get("dialog")),
    }


def clean_dialog(d) -> dict | None:
    if not isinstance(d, dict):
        return None
    msgs = [{"from": "client" if m.get("from") == "client" else "bot", "text": _clip(str(m.get("text") or ""), 160)}
            for m in (d.get("messages") or []) if isinstance(m, dict) and (m.get("text") or "").strip()][:7]
    if len(msgs) < 3:
        return None
    return {"bot_name": _clip(str(d.get("bot_name") or "Бот компании"), 34), "time": str(d.get("time") or "")[:5],
            "messages": msgs}


def used_sources(body: str, facts: list[dict], limit: int = 4) -> list[dict]:
    """Источники фактов, которые попали в текст (по названию источника); иначе первые из списка."""
    low = (body or "").lower()
    with_url = [f for f in facts if f.get("url")]
    used = [f for f in with_url if f.get("source") and f["source"].lower()[:20] in low]
    out, seen = [], set()
    for f in used or with_url:
        if f["url"] not in seen:
            seen.add(f["url"])
            out.append({"source": f.get("source") or f["url"], "url": f["url"]})
    return out[:limit]


AUTHOR_SCENE_HINT = images.AUTHOR_SCENE_HINT


looks_like_copy = images.looks_like_copy  # совместимость


def picture(cfg: Config, db: DB, ai: AI, image: dict) -> tuple["object", str]:
    """Картинка к посту: иллюстрация по смыслу или автор по его фото «реф»."""
    images._spend_image_budget(cfg, db)
    if image.get("mode") == "author":
        refs = images.pick_refs(db)
        return images.author_image(cfg, ai, refs, image)
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


SHORTEN_SYSTEM = """Ты — редактор. Сократи пост до заданного числа знаков (это жёсткий лимит площадки для подписи
к картинке). Сохрани: первую цепляющую строку, прямой ответ на тему, самые сильные факты с источником
(«по данным …»), один практический шаг и призыв в конце. Убери повторы, вводные слова и второстепенные
примеры. Новых фактов не добавляй. Отвечай ТОЛЬКО JSON: {"body": "сокращённый текст"}"""


def _trim_paragraphs(body: str, limit: int) -> str:
    """Запасной вариант без нейросети: оставляем первый абзац и призыв, середину убираем с конца."""
    paras = [p for p in body.split("\n\n") if p.strip()]
    while len("\n\n".join(paras)) > limit and len(paras) > 2:
        paras.pop(-2)
    text = "\n\n".join(paras)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
    return (cut[: end + 1] if end > limit // 2 else cut.rstrip()) + ("" if end > limit // 2 else "…")


def fit_caption(cfg: Config, ai: AI, post: dict, platform: str | None) -> tuple[str, dict]:
    """Подпись в лимите площадки: сначала без блока источников, затем сокращение редактором,
    в крайнем случае — без нейросети по абзацам. Возвращает (подпись, пост с итоговым текстом)."""
    budget = platforms.text_budget(platform)
    caption = compose_caption(post, platform)
    if len(caption) <= budget:
        return caption, post
    lean = {**post, "sources": []}  # в тексте источники уже названы: «по данным …»
    caption = compose_caption(lean, platform)
    if len(caption) <= budget:
        return caption, lean
    tags = len(" ".join(post.get("hashtags") or [])) + 2
    target = budget - tags - 20
    body = post["body"]
    for attempt in range(2):
        try:
            res = _ask(ai, cfg, SHORTEN_SYSTEM,
                       f"Лимит: {target - 60 * attempt} знаков (сейчас {len(body)}).\n\nПОСТ:\n{body}")
            short = str(res.get("body") or "").strip()
        except AIError as e:
            log.warning("shorten failed: %s", e)
            break
        if short and len(short) <= target:
            body = short
            break
        body = short or body
    if len(body) > target:
        body = _trim_paragraphs(body, target)
    lean = {**lean, "body": body}
    return compose_caption(lean, platform), lean


def compose_caption(post: dict, platform: str | None = None) -> str:
    """Текст поста; в Telegram и VK — с источниками (там ссылки работают), в Instagram — без ссылок."""
    text = post["body"].strip()
    if platform in ("tg", "vk") and post.get("sources"):
        text += "\n\nИсточники:\n" + "\n".join(f"— {s['source']}: {s['url']}" for s in post["sources"])
    tags = " ".join(post["hashtags"])
    return text + (f"\n\n{tags}" if tags else "")


def cta_fallback(cfg: Config, platform: str | None) -> dict:
    bot = (cfg.bot_username or "").lstrip("@")
    if platform == "ig" or not bot:
        return {"title": "Сохраните, чтобы *не потерять*",
                "text": "Больше разборов и бесплатный чек-лист «12 задач для бота» — по ссылке в профиле."}
    return {"title": "Бесплатный *разбор* вашей задачи",
            "text": f"Чек-лист «12 задач для бота» и бесплатный разбор вашего процесса — в боте @{bot}."}


def render_slides(cfg: Config, post: dict, token: str, platform: str | None, brand: str) -> list[str]:
    """Слайды 2..N карусели (обложка — отдельно). Файлы <token>_<n>.jpg рядом с обложкой."""
    slides = post.get("slides") or []
    cta = post.get("cta_slide") or cta_fallback(cfg, platform)
    total = len(slides) + 2
    paths = []
    for i, sl in enumerate(slides, start=2):
        out = cfg.media_dir / f"{token}_{i}.jpg"
        postcard.slide(sl["title"], sl["text"], out, i, total, brand=brand, source=sl.get("source", ""))
        paths.append(str(out))
    out = cfg.media_dir / f"{token}_{total}.jpg"
    postcard.slide(cta.get("title") or cta_fallback(cfg, platform)["title"], cta.get("text") or "", out,
                   total, total, brand=brand, last=True)
    paths.append(str(out))
    return paths


def make_post(cfg: Config, db: DB, ai: AI, request: dict, kind: str = "post") -> int:
    """Пост с картинкой (kind="post") или карусель (kind="carousel"). Сохраняет со статусом 'rendered'."""
    post = write(cfg, db, ai, request, kind)
    if kind == "carousel" and len(post["slides"]) < 3:
        raise BrainError("копирайтер не написал слайды карусели — попробуйте ещё раз")
    if kind == "dialog" and not post["dialog"]:
        raise BrainError("копирайтер не написал переписку — попробуйте ещё раз")
    token = secrets.token_urlsafe(18)
    out = cfg.media_dir / f"{token}.jpg"
    if kind == "dialog":  # картинка — макет чата, нейросеть для изображения не нужна
        post["image"] = {"mode": "dialog"}
        provider = "dialog"
        postcard.dialog_card(post["headline"], post["dialog"], out)
    else:
        try:
            im, provider = picture(cfg, db, ai, post["image"])
        except AIError as e:
            raise BrainError(f"картинка не получилась: {e}") from None
        postcard.compose(im, post["headline"], post["subline"], out,
                         author_photo=post["image"].get("mode") == "author")
    brand = ""  # подпись автора на картинках не нужна: она не кликается и ничего не добавляет
    slides = render_slides(cfg, post, token, request.get("platform"), brand) if kind == "carousel" else []
    caption, post = fit_caption(cfg, ai, post, request.get("platform"))
    script = {"script": {"hook": post["headline"]}, "post_caption": caption, "llm_ok": True, "slides": slides,
              "brain": {k: post[k] for k in ("stage", "segment", "insight", "objection", "hooks", "critic", "mechanic",
                                      "research", "sources", "slides", "dialog")}
              | {"image_mode": post["image"]["mode"], "image_provider": provider, "subline": post["subline"],
                 "body": post["body"]}}
    return db.create_post(
        target=request.get("platform"), topic_id=request.get("topic_id"),
        status="rendered", format=kind, hook_id=f"post_{post['mechanic']}",
        theme=f"stage_{post['stage']}", audience=post["segment"], explore=0,
        script_json=json.dumps(script, ensure_ascii=False), asset_ids_json=[],
        video_path=None, cover_path=str(out), caption=caption, media_token=token,
    )


# ------------------------------------------------------------------ ролики эксперта
REEL_CRITIC_SCHEMA = """Схема ответа (те же поля, исправленные; число подписей — ровно как было):
{
  "fixes": ["что исправлено", ...],
  "hook": "до 60 символов, 1–2 слова в *звёздочках*, без кавычек и тире",
  "captions": ["подпись кадра 1 — тот же смысл кадра, до %d символов", ...],
  "cta_title": "до 24 символов, одно слово в *звёздочках*",
  "cta_sub": "до 55 символов",
  "post_caption": "текст поста к ролику"
}"""


def reel_context(cfg: Config, db: DB, ai: AI, request: dict) -> dict:
    """Для ролика эксперта: факты по теме, этап воронки, возражения, можно ли показать автора."""
    strat = strategy()
    recent = _recent(db)
    stage = request.get("stage") or pick_stage(recent, strat)
    st = strat["stages"][stage]
    found = research.run(cfg, ai, request)
    return {
        "facts_block": research.facts_block(found),
        "facts": found.get("facts", []),
        "research_method": found.get("method", ""),
        "stage": stage,
        "stage_text": f"{st['title']} — {st['goal']}. Призыв: {st['cta']}. "
                      f"ПРИЗЫВ НА ЭТОЙ ПЛОЩАДКЕ: {platform_cta(strat, request.get('platform'))}",
        "objections": strat["objections"][:4],
        "author_ok": bool(images.pick_refs(db)),
        "kw_hint": keywords.prompt_hint(db) if request.get("platform") == "ig" else "",
    }


def polish_reel(cfg: Config, ai: AI, plan, request: dict):
    """Редактор-критик для сценария ролика: хук раскрыт, тезисы с фактами, цифры только из фактов."""
    from . import planner
    strat = strategy()
    nic = niche.load(cfg.niche)
    cap = planner.caption_limit(nic)
    sc = plan.script
    draft = {"hook": sc.get("hook"), "captions": sc.get("captions"), "cta_title": sc.get("cta_title"),
             "cta_sub": sc.get("cta_sub"), "post_caption": plan.post_caption}
    pl = platforms.get(request.get("platform")) or platforms.get("ig")
    prompt = "\n".join([
        f"Тема ролика: {request.get('title', '')}",
        f"Этап воронки: {request.get('stage_text', '')}",
        f"Площадка: {pl['rules']}",
        request.get("facts_block") or "",
        "Если в тексте есть цифра, которой нет в фактах, — убери её или замени фактом из списка.",
        "Подписи идут по кадрам: каждая должна остаться про свой кадр, но стать законченным тезисом.",
        "", "ЧЕК-ЛИСТ:", *[f"  {i + 1}. {c}" for i, c in enumerate(strat["critic"])],
        "", "ЧЕРНОВИК:", json.dumps(draft, ensure_ascii=False, indent=1), "", REEL_CRITIC_SCHEMA % cap,
    ])
    try:
        res = _ask(ai, cfg, CRITIC_SYSTEM + "\n\nБРИФ:\n" + (brief.load(cfg) or ""), prompt)
    except AIError as e:
        log.warning("reel critic failed: %s", e)
        return plan
    caps = res.get("captions")
    if isinstance(caps, list) and len(caps) == len(sc.get("captions") or []):
        sc["captions"] = [planner._clip(str(c), cap) or old for c, old in zip(caps, sc["captions"])]
    if res.get("hook"):
        sc["hook"] = planner._clip(clean_headline(str(res["hook"])), planner.LIMITS["hook"])
    for k in ("cta_title", "cta_sub"):
        if res.get(k):
            sc[k] = planner._clip(str(res[k]), planner.LIMITS[k])
    text = str(res.get("post_caption") or "").replace("*", "").strip()
    if len(text) >= 80:
        plan.post_caption = text[: planner.LIMITS["post_caption"]]
    sources = used_sources(plan.post_caption, request.get("facts") or [])
    platform = request.get("platform")
    if platform in ("tg", "vk"):  # подпись к видео — в лимите площадки (в Telegram — 1024 знака)
        plan.post_caption, _ = fit_caption(cfg, ai, {"body": plan.post_caption, "hashtags": [],
                                                     "sources": sources}, platform)
    plan.script = sc
    return plan


# ------------------------------------------------------------------ одна идея — все площадки
ADAPT_SYSTEM = """Ты — редактор соцсетей в команде маркетинга эксперта. Перепиши готовый пост под другую
площадку: сохрани смысл, факты (цифры только из исходного текста и списка фактов), структуру и силу текста,
но выполни правила площадки — длину, тон, призыв, хэштеги. Не добавляй ничего, чего нет в исходнике и фактах.
Отвечай ТОЛЬКО JSON: {"body": "текст поста", "hashtags": ["#..."]}"""


def adapt_post(cfg: Config, db: DB, ai: AI, src_id: int, platform: str) -> int:
    """Тот же пост (картинка, карусель или переписка) для другой площадки: картинки копируются,
    текст переписывается по её правилам. Генерация картинок не тратится."""
    import shutil
    src = db.post(src_id)
    plan = json.loads(src["script_json"] or "{}")
    b = plan.get("brain") or {}
    pl = platforms.get(platform)
    strat = strategy()
    length = length_for(strat, platform, src["format"])
    facts = research.facts_block({"facts": (b.get("research") or {}).get("facts") or []})
    prompt = "\n".join([f"ПЛОЩАДКА: {pl['rules']}", f"ПРИЗЫВ НА ЭТОЙ ПЛОЩАДКЕ: {platform_cta(strat, platform)}",
                         f"Объём: {length}. Хэштегов: до {pl['hashtags']}.", facts,
                         keywords.prompt_hint(db) if platform == "ig" else "", "",
                         "ИСХОДНЫЙ ПОСТ:", b.get("body") or src["caption"] or ""])
    try:
        res = _ask(ai, cfg, ADAPT_SYSTEM + "\n\nБРИФ:\n" + (brief.load(cfg) or ""), prompt)
    except AIError as e:
        raise BrainError(f"не удалось переписать текст для {platforms.label(platform)}: {e}") from None
    body = _clip(str(res.get("body") or ""), pl["caption_limit"] - 200)
    if len(body) < 80:
        raise BrainError(f"пустой текст для {platforms.label(platform)}")
    tags = [t if str(t).startswith("#") else f"#{t}" for t in (res.get("hashtags") or []) if t][: pl["hashtags"]]
    caption, fitted = fit_caption(cfg, ai, {"body": body, "hashtags": tags, "sources": b.get("sources") or []},
                                  platform)
    body = fitted["body"]

    token = secrets.token_urlsafe(18)
    cover = cfg.media_dir / f"{token}.jpg"
    shutil.copyfile(src["cover_path"], cover)
    slides = []
    for i, path in enumerate(plan.get("slides") or [], start=2):
        dst = cfg.media_dir / f"{token}_{i}.jpg"
        shutil.copyfile(path, dst)
        slides.append(str(dst))
    plan.update({"slides": slides, "post_caption": caption})
    plan["brain"] = {**b, "body": body, "adapted_from": src_id}
    return db.create_post(
        target=platform, topic_id=src["topic_id"], status="rendered", format=src["format"],
        hook_id=src["hook_id"], theme=src["theme"], audience=src["audience"], explore=0,
        script_json=json.dumps(plan, ensure_ascii=False), asset_ids_json=[], video_path=None,
        cover_path=str(cover), caption=caption, media_token=token,
    )
