"""Фото для роликов: разметка готовой библиотеки и генерация новых кадров.

Библиотека — твои лучшие нейрофото (папка assets/library). Один раз
нейросеть смотрит каждое фото и подписывает: короткое название сцены,
тема из плейбука, описание. Дальше ролики собираются из библиотеки
без затрат на генерацию.

Генерация — новые кадры по референсам (assets/refs) и мастер-промпту
(Часть A — ядро сходства, Часть B — сцена от планировщика).
"""
from __future__ import annotations

import io
import logging
import random
import uuid
from pathlib import Path

from PIL import Image, ImageOps

from .config import Config
from .db import DB
from .llm import AI, AIError

log = logging.getLogger(__name__)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MAX_TAG_ATTEMPTS = 3
CORE_PROMPT_PATH = Path(__file__).with_name("prompts") / "core.md"

TAG_SYSTEM = """Ты размечаешь фотографии для контент-плана. Смотришь на фото и отвечаешь JSON:
{"title": "название сцены по-русски, 2–3 слова, до 22 символов, без кавычек",
 "theme": "одна тема из списка",
 "description": "одно предложение: где человек, во что одет, настроение"}
Не описывай внешность человека, только сцену и образ. Отвечай только JSON."""


def core_prompt(cfg: Config) -> str:
    """Часть A. Если положить свой файл в DATA_DIR/core_prompt.md — используется он."""
    custom = cfg.data_dir / "core_prompt.md"
    return (custom if custom.exists() else CORE_PROMPT_PATH).read_text(encoding="utf-8")


def scene_prompt(scene: dict) -> str:
    """Часть B — кадр из 4 условий плюс свет."""
    return (
        "ЧАСТЬ B. КАДР\n"
        "B1. План: по пояс или в рост, вертикальный кадр 9:16, лицо в верхней трети кадра, "
        "крупно и хорошо освещено.\n"
        f"B2. Свет и стиль: {scene.get('light') or 'кинематографичный мягкий свет'}.\n"
        f"B3. Сцена:\n"
        f"  Где: {scene.get('where', '')}\n"
        f"  Во что одет: {scene.get('wearing', '')}\n"
        f"  Что делает: {scene.get('doing', '')}\n"
        f"  Что вокруг: {scene.get('around', '')}\n"
        "B4. Перед выдачей ещё раз сверь лицо с фото человека."
    )


def scan(cfg: Config, db: DB) -> dict[str, int]:
    """Регистрирует новые файлы из папок library / before / refs."""
    added = {}
    for kind in ("library", "before", "refs"):
        n = 0
        for p in sorted((cfg.assets_dir / kind).glob("*")):
            if p.suffix.lower() in IMAGE_EXT and not db.one("SELECT 1 FROM assets WHERE path=?", (str(p),)):
                db.add_asset(str(p), "ref" if kind == "refs" else kind)
                n += 1
        added[kind] = n
    return added


def _thumb(path: str, size: int = 768) -> bytes:
    with Image.open(path) as src:
        im = ImageOps.exif_transpose(src).convert("RGB")
    im.thumbnail((size, size))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


class TaggingError(AIError):
    """Разметка не сработала ни для одного фото — проблема общая (ключ, модель, сеть)."""


def tag_library(cfg: Config, db: DB, ai: AI, themes: dict[str, dict], limit: int = 50) -> int:
    """Подписывает неразмеченные фото библиотеки. Возвращает число размеченных.

    Если не удалось разметить ни одного фото, это общая проблема (ключ, модель,
    сеть), а не проблема фото: попытки не засчитываются, бросается TaggingError
    с текстом ошибки, чтобы владелец её увидел. Попытка засчитывается фото, только
    когда соседние фото в том же прогоне размечаются нормально.
    """
    # Сначала новые фото: по ним видно, работает ли разметка вообще.
    rows = db.q("SELECT * FROM assets WHERE kind='library' AND title IS NULL AND tag_attempts < ? "
                "ORDER BY tag_attempts, id LIMIT ?", (MAX_TAG_ATTEMPTS, limit))
    if not rows:
        return 0
    theme_list = "\n".join(f"- {k}: {v['title']}" for k, v in themes.items())
    tmp = cfg.data_dir / "work"
    tmp.mkdir(parents=True, exist_ok=True)
    done, failed = 0, []
    for row in rows:
        thumb = tmp / f"tag_{row['id']}.jpg"
        try:
            thumb.write_bytes(_thumb(row["path"]))
            res = ai.json(TAG_SYSTEM, f"Темы:\n{theme_list}", images=[thumb], temperature=0.2)
            if not isinstance(res, dict):
                raise AIError("ответ разметки не объект")
        except (AIError, OSError) as e:
            log.warning("tagging %s failed: %s", row["path"], e)
            failed.append((row["id"], e, row["tag_attempts"]))
            if done == 0 and len(failed) >= 2 and all(f[2] == 0 for f in failed):
                break  # первые два подряд не прошли — скорее всего, сломано всё; не тратим запросы
            continue
        finally:
            thumb.unlink(missing_ok=True)
        theme = res.get("theme")
        if theme not in themes:
            theme = next(iter(themes))
        title = str(res.get("title") or "Новый образ")[:24]
        db.x("UPDATE assets SET title=?, theme=?, description=? WHERE id=?",
             (title, theme, str(res.get("description") or "")[:300], row["id"]))
        done += 1
    # Общий сбой — когда не прошло ни одно фото и среди упавших есть новые.
    # Фото, которое уже падало, пока соседние размечались, — проблемное само по себе.
    if failed and done == 0 and any(f[2] == 0 for f in failed):
        raise TaggingError(f"не получилось разметить фото: {failed[0][1]}")
    for aid, _, _ in failed:
        db.x("UPDATE assets SET tag_attempts=tag_attempts+1 WHERE id=?", (aid,))
    return done


def untagged_count(db: DB) -> int:
    return db.one("SELECT COUNT(*) n FROM assets WHERE kind='library' AND title IS NULL")["n"]


def pick_library(db: DB, theme: str, n: int, rng: random.Random,
                 weights: dict[str, float] | None = None) -> tuple[list, str]:
    """n фото из библиотеки: сначала из темы, наименее использованные.

    Возвращает (фото, фактическая тема). Если в теме мало фото — берёт
    тему, где их хватает; если нигде не хватает — смесь (тема "mixed"),
    куда при нужде попадают и неразмеченные фото.
    """
    library = db.assets("library")
    tagged = [r for r in library if r["title"]]
    by_theme: dict[str, list] = {}
    for r in tagged:
        by_theme.setdefault(r["theme"], []).append(r)

    def take(rows):
        rows = sorted(rows, key=lambda r: (r["used_count"], rng.random()))
        picked = rows[:n]
        rng.shuffle(picked)
        return picked

    if len(by_theme.get(theme, [])) >= n:
        return take(by_theme[theme]), theme

    def allowed(t: str | None) -> bool:
        return t is None or weights is None or weights.get(t, 0) > 0

    enough = [t for t, rows in by_theme.items() if len(rows) >= n and allowed(t)]
    if enough:
        w = [weights[t] for t in enough] if weights else None
        t = rng.choices(enough, weights=w, k=1)[0]
        return take(by_theme[t]), t
    pool = [r for r in tagged if allowed(r["theme"])]
    if len(pool) < n:
        # Размеченных мало — добираем неразмеченными, чтобы ролик всё равно собрался.
        pool += [r for r in library if not r["title"]]
    if len(pool) >= 3:
        return take(pool), "mixed"
    return [], theme


def pick_refs(db: DB, k: int = 8) -> list[str]:
    refs = [r["path"] for r in db.assets("ref")]
    return refs[:k]


def generate(cfg: Config, db: DB, ai: AI, scene: dict, theme: str) -> int:
    """Генерирует кадр по сцене. Возвращает id нового фото в базе."""
    refs = pick_refs(db)
    if not refs:
        raise AIError("нет референсов: положи свои фото в assets/refs")
    prompt = f"{core_prompt(cfg)}\n\n{scene_prompt(scene)}"
    # Оригиналы «файлом» бывают по 5–20 МБ: 8 таких в одном запросе
    # превышают лимит Gemini. Отправляем уменьшенные копии.
    work = cfg.data_dir / "work" / uuid.uuid4().hex[:8]
    work.mkdir(parents=True, exist_ok=True)
    small = []
    try:
        for i, ref in enumerate(refs):
            dst = work / f"ref{i}.jpg"
            try:
                dst.write_bytes(_thumb(ref, 1536))
            except OSError as e:  # битый файл — пропускаем, остальные референсы работают
                log.warning("референс %s не читается: %s", ref, e)
                continue
            small.append(dst)
        if not small:
            raise AIError("ни один референс не читается — пришли их заново")
        res = ai.image(prompt, small)
    finally:
        for f in work.glob("*"):
            f.unlink(missing_ok=True)
        work.rmdir()
    im = Image.open(io.BytesIO(res.data)).convert("RGB")
    out = cfg.assets_dir / "generated" / f"{uuid.uuid4().hex[:12]}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "JPEG", quality=92)
    return db.add_asset(str(out), "generated", theme=theme, title=scene.get("title"),
                        description=f"{scene.get('where', '')}; {scene.get('wearing', '')}")
