"""Бриф продукта и аудитории — то, что сценарист и разведчик знают о продукте.

Стандартный бриф лежит в engine/prompts/brief.md. Свой можно прислать
админ-боту файлом .md или .txt с подписью «бриф» — он сохранится на томе
(DATA_DIR/brief.md) и будет важнее стандартного.
"""
from __future__ import annotations

from pathlib import Path

from .config import Config

DEFAULT = Path(__file__).with_name("prompts") / "brief.md"
MAX_CHARS = 12000


def path(cfg: Config) -> Path:
    return cfg.data_dir / "brief.md"


def load(cfg: Config) -> str:
    custom = path(cfg)
    src = custom if custom.exists() else DEFAULT
    return src.read_text(encoding="utf-8")[:MAX_CHARS]


def is_custom(cfg: Config) -> bool:
    return path(cfg).exists()


def save(cfg: Config, text: str) -> None:
    text = text.strip()
    if len(text) < 200:
        raise ValueError("бриф слишком короткий — нужно хотя бы несколько абзацев")
    if len(text) > MAX_CHARS:
        raise ValueError(f"бриф слишком длинный: {len(text)} символов, максимум {MAX_CHARS}")
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def reset(cfg: Config) -> None:
    path(cfg).unlink(missing_ok=True)


NOTES_HEADER = "## Уточнения по статистике аккаунта"


def append_note(cfg: Config, note: str) -> None:
    """Добавляет одобренное уточнение в бриф (создаёт свой бриф из стандартного, если его ещё нет)."""
    text = load(cfg)
    if NOTES_HEADER not in text:
        text = text.rstrip() + f"\n\n{NOTES_HEADER}\n"
    text = text.rstrip() + f"\n- {note.strip()}\n"
    if len(text) > MAX_CHARS:
        raise ValueError("бриф стал слишком длинным — сократи его вручную (/brief) и пришли файлом")
    p = path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
