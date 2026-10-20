"""Планировщик: превращает выбор из плейбука в сценарий ролика.

Нейросеть пишет хук, подписи к кадрам, финальный призыв, текст поста
и (в режиме генерации) описания сцен. Ответ проверяется и обрезается
по длинам; если модель недоступна — собирается безопасный шаблон,
чтобы конвейер не вставал.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field

from .llm import AI, AIError
from .playbook import Choice
from .video.templates import Script

log = logging.getLogger(__name__)

SYSTEM = """Ты — сценарист коротких вертикальных видео (Reels, Shorts) и директ-маркетолог:
вирусность, вовлечение и конверсия. Аккаунт: нейрофотограф. Автор показывает собственные фото,
созданные его Telegram-ботом @{bot}.
Главная цель каждого ролика — конверсия: зритель досмотрел, узнал в ролике свою ситуацию,
переслал другу или сохранил, перешёл в бот по ссылке в профиле и сделал первое фото.

Пиши для конкретного сегмента аудитории, указанного в задании: его ситуация, его боль, его
желание, сними его возражение. Хук — про зрителя и его жизнь, а не про инструмент.

БРИФ ПРОДУКТА И АУДИТОРИИ (источник правды о продукте; не выдумывай ничего сверх него):
{brief}

Правила, которые нельзя нарушать:
{rules}

Пиши по-русски, живо и коротко, без канцелярита и без эмодзи в текстах на видео.
Отвечай ТОЛЬКО JSON по заданной схеме."""

SCHEMA_HINT = """Схема ответа:
{{
  "hook": "заголовок на первые 2 секунды, до 60 символов, по мотивам паттерна хука; 1–2 ключевых слова оберни в *звёздочки* — они будут выделены цветом",
  "captions": ["подпись к кадру 1, до 22 символов", ... ровно {n} шт.],
  {scenes_hint}"cta_title": "финальный призыв, до 24 символов, одно ключевое слово в *звёздочках*",
  "cta_sub": "подзаголовок призыва, до 55 символов, упомянуть бот по ссылке в профиле",
  "post_caption": "текст поста 250–700 символов: первая строка цепляет ситуацией сегмента; дальше короткая история или польза и снятие возражения; повод переслать ролик конкретному другу; вопрос для комментариев; призыв перейти в бот по ссылке в профиле",
  "hashtags": ["5–8 хештегов на русском и английском, с #"]
}}"""

SCENES_HINT = """"scenes": [{{"title": "та же подпись, что в captions", "where": "где", "wearing": "во что одет", "doing": "что делает", "around": "что вокруг", "light": "свет и стиль кадра"}}, ... ровно {n} шт.],
  """

LIMITS = {"hook": 70, "caption": 24, "cta_title": 28, "cta_sub": 60, "post_caption": 1500}


@dataclass
class Plan:
    choice: dict
    script: dict                      # поля Script
    scenes: list[dict] = field(default_factory=list)
    post_caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    llm_ok: bool = True
    llm_error: str = ""

    def to_script(self) -> Script:
        return Script(**self.script)

    def full_caption(self) -> str:
        tags = " ".join(self.hashtags)
        return f"{self.post_caption}\n\n{tags}".strip()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "Plan":
        return cls(**json.loads(s))


def theme_info(pb: dict, theme: str) -> dict:
    """Описание темы; для смешанной подборки — общее."""
    return pb["themes"].get(theme) or {"title": "разные образы", "scenes": []}


def _clip(text: str, limit: int) -> str:
    """Обрезает по словам. Длина считается без *звёздочек* выделения;
    непарная звёздочка после обрезки убирается."""
    text = " ".join(str(text or "").split())
    if len(text.replace("*", "")) > limit:
        words, out = text.split(" "), []
        for w in words:
            if len(" ".join(out + [w]).replace("*", "")) > limit:
                break
            out.append(w)
        text = (" ".join(out).rstrip(",.;:—-") + "…") if out else text[:limit]
    if text.count("*") % 2:
        text = " ".join(text.replace("*", "").split())
    return text


def build_prompt(pb: dict, choice: Choice, n: int, photo_notes: list[str] | None,
                 generate_scenes: bool, gender: str) -> str:
    fmt = pb["formats"][choice.format]
    hook = pb["hooks"][choice.hook_id]
    theme = theme_info(pb, choice.theme)
    lines = [
        f"Формат ролика: {choice.format} — {fmt['about']}.",
        f"Паттерн хука: «{hook['pattern']}» (идея: {hook['idea']}). Перепиши своими словами под тему, сохрани механику.",
        f"Тема кадров: {theme['title']}." + (f" Примеры сцен темы: {'; '.join(theme['scenes'])}." if theme["scenes"] else ""),
        f"Кадров в ролике: {n}.",
        f"Стиль текста на видео: {pb.get('styles', {}).get(choice.style, {}).get('about', choice.style)}.",
        *([f"Целевой сегмент аудитории: {pb['audiences'][choice.audience]['about']}. "
           "Пиши для этих людей: их ситуация, боль, желание и возражение — по брифу."]
          if choice.audience and choice.audience in pb.get("audiences", {}) else []),
        f"Автор: {'мужчина' if gender == 'male' else 'женщина'}, говорит от первого лица.",
    ]
    if theme.get("occasion"):
        lines.append(f"ПОВОД РОЛИКА: {theme['occasion']}. Все кадры, хук и текст — в тему этого повода. "
                     "Свяжи повод с ботом: своё фото к празднику за пару минут, без студии и записи. "
                     "Поздравить аудиторию уместно, но коротко и без пафоса.")
        if theme.get("profession"):
            lines.append("Подача «я в профессии»: автор примеряет образ представителя профессии и "
                         "поздравляет тех, кто в ней работает; предложи им сделать такое же фото.")
        if theme.get("timing"):
            lines.append(f"Время: {theme['timing']}")
        if theme.get("note"):
            lines.append(f"Важно: {theme['note']}")
    elif choice.occasion:
        lines.append(f"Сейчас сезон: {choice.occasion}. Обыграй повод, если уместно.")
    if photo_notes:
        lines.append("Фото уже готовы, подписи должны им соответствовать, по порядку:")
        lines += [f"  {i + 1}. {note}" for i, note in enumerate(photo_notes)]
    if pb.get("trend_notes"):
        lines.append("Что сейчас в тренде в Reels и Shorts (свежая разведка, используй, если уместно):")
        lines += [f"  — {x}" for x in pb["trend_notes"]]
    if pb.get("insights"):
        lines.append("Что мы уже знаем по статистике аккаунта:")
        lines += [f"  — {x}" for x in pb["insights"]]
    lines.append("")
    lines.append(SCHEMA_HINT.format(n=n, scenes_hint=SCENES_HINT.format(n=n) if generate_scenes else ""))
    return "\n".join(lines)


def validate(raw: dict, choice: Choice, pb: dict, n: int, generate_scenes: bool) -> Plan:
    if not isinstance(raw, dict):
        raise AIError("ответ не объект")
    hook = _clip(raw.get("hook") or pb["hooks"][choice.hook_id]["pattern"], LIMITS["hook"])
    captions = [_clip(c, LIMITS["caption"]) for c in (raw.get("captions") or [])][:n]
    scenes: list[dict] = []
    if generate_scenes:
        for sc in (raw.get("scenes") or [])[:n]:
            if isinstance(sc, dict) and sc.get("where"):
                scenes.append({k: _clip(sc.get(k, ""), 300) for k in ("title", "where", "wearing", "doing", "around", "light")})
        if len(scenes) < n:
            raise AIError(f"сцен {len(scenes)} из {n}")
        captions = [_clip(sc["title"], LIMITS["caption"]) or c for sc, c in
                    zip(scenes, captions + [""] * (n - len(captions)))]
    captions += [""] * (n - len(captions))
    tags = [t if t.startswith("#") else f"#{t}" for t in (raw.get("hashtags") or []) if isinstance(t, str)]
    tags = [t.replace(" ", "") for t in tags][:8]
    script = asdict(Script(
        hook=hook,
        captions=captions,
        cta_title=_clip(raw.get("cta_title") or "Хочешь так же?", LIMITS["cta_title"]),
        cta_sub=_clip(raw.get("cta_sub") or "Бот-нейрофотограф — по ссылке в профиле", LIMITS["cta_sub"]),
    ))
    caption = str(raw.get("post_caption") or "").replace("*", "").strip()[: LIMITS["post_caption"]]
    if len(caption) < 40:
        raise AIError("слишком короткий текст поста")
    return Plan(choice=asdict(choice), script=script, scenes=scenes, post_caption=caption, hashtags=tags)


def fallback(pb: dict, choice: Choice, n: int, photo_notes: list[str] | None, bot: str) -> Plan:
    """Шаблонный сценарий без нейросети — чтобы конвейер не вставал."""
    theme = theme_info(pb, choice.theme)
    notes = photo_notes or theme["scenes"] or ["Новый образ"]
    captions = [_clip(x, LIMITS["caption"]) for x in (notes * n)[:n]]
    caption = (f"{pb['hooks'][choice.hook_id]['pattern']}.\n\n"
               f"Все кадры — мои, сделаны ботом @{bot} из обычных селфи. "
               "Присылаешь несколько своих фото, выбираешь сцену — получаешь новый кадр.\n\n"
               "Какой образ тебе ближе? Напиши номер в комментариях.\n"
               "Опиши свою сцену — бот по ссылке в профиле.")
    script = asdict(Script(hook=_clip(pb["hooks"][choice.hook_id]["pattern"], LIMITS["hook"]), captions=captions))
    return Plan(choice=asdict(choice), script=script, post_caption=caption,
                hashtags=["#нейрофотосессия", "#нейрофото", "#фотосессия", "#aiphoto", "#нейросеть"],
                llm_ok=False)


def plan(ai: AI, pb: dict, choice: Choice, n: int, *, photo_notes: list[str] | None = None,
         generate_scenes: bool = False, gender: str = "male", bot: str = "Easyfotobot",
         brief_text: str = "") -> Plan:
    system = SYSTEM.format(bot=bot, brief=brief_text or "(бриф не задан)",
                           rules="\n".join(f"- {r}" for r in pb.get("rules", [])))
    prompt = build_prompt(pb, choice, n, photo_notes, generate_scenes, gender)
    last_err: Exception | None = None
    for _ in range(2):
        try:
            return validate(ai.json(system, prompt), choice, pb, n, generate_scenes)
        except (AIError, ValueError, TypeError, KeyError) as e:
            last_err = e
            log.warning("planner retry: %s", e)
    if generate_scenes:
        # Без описаний сцен генерировать нечего — пусть решает вызывающий код.
        raise AIError(f"планировщик не смог составить сцены: {last_err}")
    log.error("planner fallback: %s", last_err)
    fb = fallback(pb, choice, n, photo_notes, bot)
    fb.llm_error = str(last_err)[:300]
    return fb
