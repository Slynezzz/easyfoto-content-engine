"""Музыка для роликов: твои треки по настроениям.

Треки присылаются админ-боту (как аудио или файлом) с подписью
«музыка <настроение>», например «музыка мистика». Хранятся на томе
в DATA_DIR/music/<настроение>/. Ролик к поводу берёт трек под настроение
повода, обычный ролик — любой подходящий.

Instagram через API не даёт подставить трендовый звук из своей
библиотеки, поэтому используем только треки без авторских ограничений
(например, из фонотеки YouTube Studio).
"""
from __future__ import annotations

import random
from pathlib import Path

from .config import Config

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg"}
REPO_MUSIC = Path(__file__).resolve().parents[1] / "assets" / "music"

# настроение -> слова, по которым его узнать в подписи
MOODS = {
    "festive": ("праздн", "новогод", "festive"),
    "romantic": ("романт", "любов", "romantic"),
    "epic": ("эпик", "драйв", "мощн", "epic"),
    "mystic": ("мисти", "хэллоуин", "хеллоуин", "тёмн", "темн", "mystic"),
    "warm": ("тёпл", "тепл", "уют", "warm"),
    "fun": ("весёл", "весел", "юмор", "смешн", "fun"),
    "upbeat": ("бодр", "энерг", "ритм", "upbeat"),
    "adventure": ("путеш", "приключ", "adventure"),
    "confident": ("делов", "уверен", "confident"),
    "folk": ("народ", "folk"),
    "calm": ("спокой", "лаунж", "calm"),
}
MOOD_RU = {"festive": "праздничная", "romantic": "романтичная", "epic": "эпичная", "mystic": "мистика",
           "warm": "тёплая", "fun": "весёлая", "upbeat": "бодрая", "adventure": "путешествия",
           "confident": "деловая", "folk": "народная", "calm": "спокойная",
           "trend": "трендовая (создана нейросетью)"}
# если под настроение повода нет трека — какое настроение взять вместо
FALLBACK = {"festive": "upbeat", "romantic": "warm", "epic": "upbeat", "mystic": "epic", "warm": "calm",
            "fun": "upbeat", "adventure": "upbeat", "confident": "calm", "folk": "festive", "calm": "warm"}


def mood_from_caption(caption: str) -> str:
    low = (caption or "").lower()
    for mood, keys in MOODS.items():
        if any(k in low for k in keys):
            return mood
    return "upbeat"


def music_dir(cfg: Config) -> Path:
    return cfg.data_dir / "music"


def tracks(cfg: Config) -> dict[str, list[Path]]:
    """Все треки по настроениям (с тома и из репозитория)."""
    out: dict[str, list[Path]] = {}
    for root in (music_dir(cfg), REPO_MUSIC):
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if p.suffix.lower() in AUDIO_EXT and p.is_file():
                mood = p.parent.name if p.parent != root else "upbeat"
                out.setdefault(mood, []).append(p)
    return out


def pick(cfg: Config, mood: str | None, rng: random.Random) -> Path | None:
    lib = tracks(cfg)
    if not lib:
        return None
    for m in (mood, FALLBACK.get(mood or ""), "upbeat"):
        if m and lib.get(m):
            return rng.choice(lib[m])
    return rng.choice([t for ts in lib.values() for t in ts])


def summary(cfg: Config) -> str:
    lib = tracks(cfg)
    if not lib:
        return ("Музыки пока нет — ролики выходят без звука. Пришли сюда mp3 (как аудио или файлом) "
                "с подписью «музыка» и настроением, например «музыка мистика».")
    lines = [f"• {MOOD_RU.get(m, m)}: {len(ts)}" for m, ts in sorted(lib.items())]
    return ("Треки по настроениям:\n" + "\n".join(lines)
            + "\n\nНастроения: " + ", ".join(v for k, v in MOOD_RU.items() if k != "trend") + ".")
