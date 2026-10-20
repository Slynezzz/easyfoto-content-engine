"""Музыка, созданная нейросетью под ролик.

Трендовые звуки из библиотек Instagram и TikTok через API подставить нельзя,
а чужой трек, наложенный в ролик, площадка заглушит или заблокирует: подпись
автора прав не даёт. Поэтому под каждый ролик генерируется своя мелодия в
стиле, который сейчас в трендах коротких видео (стили приносит разведчик),
или под настроение повода. Права на такие треки — у владельца платного тарифа
ElevenLabs, отметки автора не нужны.

Созданные треки сохраняются в фонотеку (DATA_DIR/music/<настроение>/), так что
после дневного лимита ролики берут уже готовые.
"""
from __future__ import annotations

import logging
import random
import secrets
from datetime import date
from pathlib import Path

import requests

from . import music
from .config import Config
from .db import DB

log = logging.getLogger(__name__)


class MusicError(RuntimeError):
    pass


# стиль по настроению повода — на английском: так модели понимают точнее
MOOD_STYLE = {
    "festive": "upbeat festive pop with sleigh bells, claps and a bright synth hook, 120 bpm",
    "mystic": "dark cinematic trap with eerie choir pads, music box melody and deep 808 bass, 140 bpm",
    "romantic": "warm romantic lo-fi R&B with soft electric piano, 85 bpm",
    "epic": "epic cinematic hybrid orchestral with big drums and rising strings, 100 bpm",
    "warm": "warm acoustic indie pop with guitar, claps and whistles, 100 bpm",
    "fun": "playful quirky pop with pizzicato strings, bouncy bass and whistles, 115 bpm",
    "upbeat": "upbeat modern pop house with a catchy plucked synth hook, 122 bpm",
    "adventure": "uplifting travel indie-electronic with bright guitars, 110 bpm",
    "confident": "confident stylish deep house with a groovy bassline, 118 bpm",
    "folk": "modern folk-pop fusion with balalaika-like plucks over an electronic beat, 120 bpm",
    "calm": "chill lo-fi hip hop beat with mellow keys, 80 bpm",
}

PROMPT = ("{style}. Instrumental only, no vocals. Catchy and modern from the very first second, "
          "made as background music for a vertical short video (Instagram Reels). "
          "Energetic but leaves room for on-screen text. Clean ending.")


def configured(cfg: Config) -> bool:
    return bool(cfg.music_api_key)


def _day_key(today: date | None = None) -> str:
    return f"music_used:{(today or date.today()).isoformat()}"


def used_today(db: DB, today: date | None = None) -> int:
    return int(db.get(_day_key(today), "0") or 0)


def choose_style(pb: dict, mood: str | None, rng: random.Random) -> tuple[str, str]:
    """(стиль, настроение-папка). Повод — по его настроению; обычный ролик — трендовый стиль."""
    if mood:
        return MOOD_STYLE.get(mood, MOOD_STYLE["upbeat"]), mood
    trend = [s["style"] for s in pb.get("music_styles", []) if s.get("style")]
    if trend:
        return rng.choice(trend), "trend"
    return MOOD_STYLE["upbeat"], "upbeat"


def _looks_like_audio(data: bytes) -> bool:
    return len(data) > 8000 and (data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
                                 or data[:4] in (b"OggS", b"RIFF", b"fLaC") or data[4:8] == b"ftyp")


def generate(cfg: Config, db: DB, style: str, folder: str, seconds: float,
             today: date | None = None) -> Path:
    """Создаёт трек и кладёт его в фонотеку. Тратит дневной лимит только при успехе."""
    if not configured(cfg):
        raise MusicError("генерация музыки не подключена: нет MUSIC_API_KEY")
    if used_today(db, today) >= cfg.max_music_per_day:
        raise MusicError(f"дневной лимит новых треков ({cfg.max_music_per_day}) исчерпан")
    length_ms = int(min(60, max(10, seconds + 2)) * 1000)
    body = {"prompt": PROMPT.format(style=style), "music_length_ms": length_ms,
            "model_id": cfg.music_model, "force_instrumental": True}
    headers = {"xi-api-key": cfg.music_api_key, "Authorization": f"Bearer {cfg.music_api_key}",
               "Content-Type": "application/json"}
    try:
        r = requests.post(f"{cfg.music_api_base_url}/v1/music", json=body, headers=headers,
                          params={"output_format": "mp3_44100_128"}, timeout=240)
    except requests.RequestException as e:
        raise MusicError(f"сервис музыки недоступен: {type(e).__name__}") from None
    if r.status_code != 200:
        try:
            detail = r.json().get("detail")
            msg = detail.get("message") if isinstance(detail, dict) else str(detail)
        except ValueError:
            msg = r.text[:200]
        raise MusicError(f"сервис музыки ответил {r.status_code}: {str(msg)[:300]}")
    if not _looks_like_audio(r.content):
        raise MusicError("сервис музыки вернул не аудио")
    out = music.music_dir(cfg) / folder / f"gen_{date.today():%Y%m%d}_{secrets.token_hex(4)}.mp3"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    db.set(_day_key(today), str(used_today(db, today) + 1))
    log.info("music generated: %s (%s)", out.name, style[:60])
    return out


def track_for(cfg: Config, db: DB, pb: dict, mood: str | None, seconds: float,
              rng: random.Random) -> Path | None:
    """Трек для ролика: новый (если подключено и лимит не исчерпан), иначе из фонотеки."""
    if configured(cfg) and used_today(db) < cfg.max_music_per_day:
        style, folder = choose_style(pb, mood, rng)
        try:
            path = generate(cfg, db, style, folder, seconds)
            db.set("last_music_error", "")
            return path
        except MusicError as e:
            log.warning("music: %s", e)
            db.set("last_music_error", str(e)[:300])
    return music.pick(cfg, mood, rng)


def status(cfg: Config, db: DB) -> str:
    if not configured(cfg):
        return ("Генерация музыки выключена. Чтобы под каждый ролик создавался свой трек в трендовом стиле, "
                "добавь в Railway MUSIC_API_KEY (ключ ElevenLabs или посредника с тем же API).")
    err = db.get("last_music_error") or ""
    return (f"Генерация музыки включена ({cfg.music_model}): сегодня {used_today(db)} из {cfg.max_music_per_day} новых треков."
            + (f"\nПоследняя ошибка: {err}" if err else ""))
