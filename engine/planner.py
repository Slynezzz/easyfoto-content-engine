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

from . import niche, platforms
from .llm import AI, AIError
from .playbook import Choice
from .video.templates import Script

log = logging.getLogger(__name__)

SYSTEM = """Ты — сценарист коротких вертикальных видео (Reels, Shorts) и директ-маркетолог:
вирусность, вовлечение и конверсия. {account}
{goal}

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
  "captions": ["подпись к кадру 1, до {cap} символов", ... ровно {n} шт.],
  {scenes_hint}"cta_title": "финальный призыв, до 24 символов, одно ключевое слово в *звёздочках*",
  "cta_sub": "подзаголовок призыва, до 55 символов, {cta_sub_hint}",
  "post_caption": "текст поста 250–700 символов: первая строка цепляет ситуацией сегмента; дальше короткая история или польза и снятие возражения; повод переслать ролик конкретному другу; вопрос для комментариев; {caption_cta_hint}",
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
    copy_error: str = ""              # почему сильная модель текстов не справилась (тогда писала основная)

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


def fit_platform(plan_: "Plan", request: dict | None) -> "Plan":
    """Хэштеги — не больше, чем разрешает площадка (Instagram — 5, Telegram — без них)."""
    pl = platforms.get((request or {}).get("platform"))
    if pl:
        plan_.hashtags = plan_.hashtags[: pl["hashtags"]]
    return plan_


def request_lines(request: dict | None) -> list[str]:
    """Заказ владельца: тема ролика и площадка, под которую пишем текст."""
    if not request:
        return []
    lines = ["", "ЗАКАЗ ВЛАДЕЛЬЦА — ролик строго на эту тему, хук и тезисы раскрывают именно её:",
             f"Тема: {request.get('title', '')}"]
    if request.get("angle"):
        lines.append(f"Как подать: {request['angle']}")
    if request.get("url"):
        lines.append(f"Источник (не выдумывай фактов сверх него и брифа): {request['url']}")
    pl = platforms.get(request.get("platform"))
    if pl:
        lines.append(f"ПЛОЩАДКА: {pl['rules']}")
    if request.get("facts_block"):  # эксперт: исследование, этап воронки, возражение
        lines += ["", request["facts_block"], "",
                  "ТРЕБОВАНИЯ К РОЛИКУ: хук — обещание или вопрос по теме; подписи кадров — цепочка законченных "
                  "тезисов: ответ на хук → факты с цифрами (только из фактов выше) → что сделать → вывод; "
                  "каждая подпись понятна без звука. Текст поста раскрывает тему с фактами и источниками "
                  "(«по данным …»), без воды."]
    if request.get("kw_hint"):
        lines.append(request["kw_hint"])
    if request.get("stage_text"):
        lines.append(f"ЭТАП ВОРОНКИ: {request['stage_text']}")
    if request.get("objections"):
        lines.append("Если уместно, сними одно возражение: " + "; ".join(request["objections"]))
    if request.get("author_ok"):
        lines.append("Можно показать автора: в 1 кадре из всех (личный момент — автор за работой, у моря, "
                     "объясняет) поставь в сцене \"author\": true и опиши, что он делает и во что одет. "
                     "Остальные кадры — иллюстрации без людей крупно.")
    return lines


def build_prompt(pb: dict, choice: Choice, n: int, photo_notes: list[str] | None,
                 generate_scenes: bool, gender: str, nic: dict | None = None,
                 request: dict | None = None) -> str:
    nic = nic or niche.load("neurophoto")
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
                     + nic["occasion_hint"])
        if theme.get("profession"):
            lines.append(nic["profession_hint"])
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
    lines += request_lines(request)
    lines.append("")
    hint = (nic.get("scenes_hint") or SCENES_HINT).format(n=n)
    lines.append(SCHEMA_HINT.format(n=n, scenes_hint=hint if generate_scenes else "", cap=caption_limit(nic) - 2,
                                    cta_sub_hint=nic["cta_sub_hint"], caption_cta_hint=nic["caption_cta_hint"]))
    return "\n".join(lines)


def caption_limit(nic: dict | None) -> int:
    """Подписи на кадрах: у нейрофото — короткие (24), у эксперта — тезис с цифрой (до 48)."""
    return int((nic or {}).get("caption_limit") or LIMITS["caption"])


def validate(raw: dict, choice: Choice, pb: dict, n: int, generate_scenes: bool, nic: dict | None = None) -> Plan:
    if not isinstance(raw, dict):
        raise AIError("ответ не объект")
    nic = nic or niche.load("neurophoto")
    cap = caption_limit(nic)
    hook = _clip(raw.get("hook") or pb["hooks"][choice.hook_id]["pattern"], LIMITS["hook"])
    captions = [_clip(c, cap) for c in (raw.get("captions") or [])][:n]
    scenes: list[dict] = []
    if generate_scenes:
        for sc in (raw.get("scenes") or [])[:n]:
            if isinstance(sc, dict) and sc.get("where"):
                scenes.append({k: _clip(sc.get(k, ""), 300) for k in ("title", "where", "wearing", "doing", "around", "light")}
                              | ({"author": True} if sc.get("author") is True else {}))
        if len(scenes) < n:
            raise AIError(f"сцен {len(scenes)} из {n}")
        captions = [_clip(sc["title"], cap) or c for sc, c in
                    zip(scenes, captions + [""] * (n - len(captions)))]
    captions += [""] * (n - len(captions))
    tags = [t if t.startswith("#") else f"#{t}" for t in (raw.get("hashtags") or []) if isinstance(t, str)]
    tags = [t.replace(" ", "") for t in tags][:8]
    script = asdict(Script(
        hook=hook,
        captions=captions,
        cta_title=_clip(raw.get("cta_title") or nic["default_cta_title"], LIMITS["cta_title"]),
        cta_sub=_clip(raw.get("cta_sub") or nic["default_cta_sub"], LIMITS["cta_sub"]),
    ))
    caption = str(raw.get("post_caption") or "").replace("*", "").strip()[: LIMITS["post_caption"]]
    if len(caption) < 40:
        raise AIError("слишком короткий текст поста")
    return Plan(choice=asdict(choice), script=script, scenes=scenes, post_caption=caption, hashtags=tags)


def fallback(pb: dict, choice: Choice, n: int, photo_notes: list[str] | None, bot: str,
             nic: dict | None = None) -> Plan:
    """Шаблонный сценарий без нейросети — чтобы конвейер не вставал."""
    nic = nic or niche.load("neurophoto")
    theme = theme_info(pb, choice.theme)
    notes = photo_notes or theme["scenes"] or ["Новый образ"]
    captions = [_clip(x, LIMITS["caption"]) for x in (notes * n)[:n]]
    hook = pb["hooks"][choice.hook_id]["pattern"]
    caption = nic["fallback_caption"].format(hook=hook.rstrip(".?!"), bot=bot)
    script = asdict(Script(hook=_clip(hook, LIMITS["hook"]), captions=captions,
                           cta_title=nic["default_cta_title"], cta_sub=nic["default_cta_sub"]))
    return Plan(choice=asdict(choice), script=script, post_caption=caption,
                hashtags=list(nic["fallback_hashtags"]), llm_ok=False)


def plan(ai: AI, pb: dict, choice: Choice, n: int, *, photo_notes: list[str] | None = None,
         generate_scenes: bool = False, gender: str = "male", bot: str = "Easyfotobot",
         brief_text: str = "", copy_model: str = "", nic: dict | None = None,
         request: dict | None = None) -> Plan:
    nic = nic or niche.load("neurophoto")
    system = SYSTEM.format(account=nic["account"].format(bot=bot), goal=nic["goal"],
                           brief=brief_text or "(бриф не задан)",
                           rules="\n".join(f"- {r}" for r in pb.get("rules", [])))
    prompt = build_prompt(pb, choice, n, photo_notes, generate_scenes, gender, nic, request)
    last_err: Exception | None = None
    copy_error = ""
    if copy_model:
        # тексты пишет сильная модель; если не справилась — основная, как раньше
        try:
            return fit_platform(validate(ai.json(system, prompt, model=copy_model), choice, pb, n,
                                         generate_scenes, nic), request)
        except (AIError, ValueError, TypeError, KeyError) as e:
            copy_error = f"{copy_model}: {str(e)[:200]}"
            log.warning("copy model failed: %s", copy_error)
    for _ in range(2):
        try:
            res = fit_platform(validate(ai.json(system, prompt), choice, pb, n, generate_scenes, nic), request)
            res.copy_error = copy_error
            return res
        except (AIError, ValueError, TypeError, KeyError) as e:
            last_err = e
            log.warning("planner retry: %s", e)
    if generate_scenes:
        # Без описаний сцен генерировать нечего — пусть решает вызывающий код.
        raise AIError(f"планировщик не смог составить сцены: {last_err}")
    log.error("planner fallback: %s", last_err)
    fb = fallback(pb, choice, n, photo_notes, bot, nic)
    fb.llm_error = str(last_err)[:300]
    fb.copy_error = copy_error
    return fb
