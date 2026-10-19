"""Форматы роликов. Каждый формат превращает набор фото и тексты сценария
в VideoSpec для рендера. Новые форматы добавляются сюда и в плейбук.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

from . import skins
from . import text as tx
from .render import BAND_FRAC, H, SAFE_BOTTOM, SAFE_TOP, Overlay, Segment, VideoSpec, band_text_y

MUSIC_DIR = Path(__file__).resolve().parents[2] / "assets" / "music"


@dataclass
class Script:
    """Текстовая часть ролика — её пишет планировщик (LLM)."""
    hook: str
    captions: list[str] = field(default_factory=list)
    cta_title: str = "Хочешь так же?"
    cta_sub: str = "Бот-нейрофотограф — по ссылке в профиле"
    badge_before: str = "Было"
    badge_after: str = "Стало"
    style: str = skins.DEFAULT_SKIN   # стиль текста на видео, см. skins.SKINS


def pick_music(seed: int | None = None) -> Path | None:
    tracks = sorted(p for p in MUSIC_DIR.glob("*") if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".aac"})
    if not tracks:
        return None
    return random.Random(seed).choice(tracks)


KINETIC_STEP = 0.14  # секунд на слово при появлении по словам


def _caption(text: str, style: str, start: float = 0.15) -> Overlay:
    img = skins.render(text, style, size=60, max_width=860)
    return Overlay(img, y=SAFE_BOTTOM - img.height - 40, start=start, anim="slide_up")


def _hook(text: str, style: str, anim: str = "pop") -> list[Overlay]:
    """Заголовок для кадра с background="top_band" — встаёт в тёмную полосу.

    В стиле kinetic слова появляются по одному: несколько кадров-наложений,
    каждое следующее показывает на слово больше, раскладка не прыгает.
    """
    full = skins.render(text, style, size=90)
    y = band_text_y(full.height)
    skin = skins.get(style)
    n = skins.word_count(text)
    if not skin.kinetic or n < 2:
        return [Overlay(full, y=y, start=0.0, anim=anim)]
    out = []
    for k in range(1, n):
        out.append(Overlay(skins.render(text, style, size=90, reveal=k), y=y,
                           start=(k - 1) * KINETIC_STEP, end=k * KINETIC_STEP, anim="none", fade_out=False))
    out.append(Overlay(full, y=y, start=(n - 1) * KINETIC_STEP, anim="none"))
    return out


def _plain(text: str) -> str:
    return skins.strip_marks(text)


def _cta(image, script: Script, duration: float = 2.6) -> Segment:
    title = skins.render(script.cta_title, script.style, size=96)
    sub = tx.render_text(_plain(script.cta_sub), tx.CTA_SUB)
    return Segment(
        image=image, duration=duration, motion="still", transition="crossfade",
        transition_dur=0.35, background="blur_dark",
        overlays=[
            Overlay(title, y=720, start=0.1, anim="pop"),
            Overlay(sub, y=740 + title.height + 50, start=0.45, anim="slide_up"),
        ],
    )


def _caption_at(script: Script, i: int) -> str | None:
    return script.captions[i] if i < len(script.captions) and script.captions[i] else None


# ------------------------------------------------------------------ форматы

def before_after(before, afters: list, script: Script, music_seed: int | None = None) -> VideoSpec:
    """«Обычное селфи → серия нейрофото». Главный формат для продукта."""
    if not afters:
        raise ValueError("нужно хотя бы одно фото «после»")
    badge_b = tx.render_text(_plain(script.badge_before), tx.BADGE)
    badge_a = tx.render_text(_plain(script.badge_after), tx.BADGE)
    segs = [Segment(
        image=before, duration=2.4, background="top_band",
        overlays=[*_hook(script.hook, script.style),
                  Overlay(badge_b, y=int(H * BAND_FRAC) + 36, x=48, start=0.3, anim="pop")],
    )]
    motions = ["zoom_in", "zoom_out", "pan_up", "zoom_in", "pan_down"]
    for i, img in enumerate(afters):
        overlays = []
        if i == 0:
            overlays.append(Overlay(badge_a, y=SAFE_TOP + 60, start=0.1, anim="pop"))
        cap = _caption_at(script, i)
        if cap:
            overlays.append(_caption(cap, script.style))
        segs.append(Segment(
            image=img,
            duration=1.8 if i == 0 else 1.3,
            motion="punch" if i == 0 else motions[i % len(motions)],
            transition="flash" if i == 0 else "cut",
            overlays=overlays,
        ))
    segs.append(_cta(afters[-1], script))
    return VideoSpec(segs, music=pick_music(music_seed), cover_time=0.6)


def looks_carousel(images: list, script: Script, music_seed: int | None = None) -> VideoSpec:
    """Быстрая нарезка образов, заголовок висит весь ролик (удерживает внимание)."""
    if len(images) < 3:
        raise ValueError("для карусели нужно минимум 3 фото")
    first = _hook(script.hook, script.style)
    still = first[-1]  # полный заголовок висит на остальных кадрах
    segs = []
    for i, img in enumerate(images):
        overlays = list(first) if i == 0 else [Overlay(still.image, y=still.y, anim="none")]
        cap = _caption_at(script, i)
        if cap:
            overlays.append(_caption(cap, script.style, start=0.05))
        segs.append(Segment(
            image=img,
            duration=2.0 if i == 0 else 1.0,
            background="top_band",
            transition="cut",
            overlays=overlays,
        ))
    segs.append(_cta(images[-1], script))
    return VideoSpec(segs, music=pick_music(music_seed), cover_time=0.5)


def worlds(images: list, script: Script, music_seed: int | None = None) -> VideoSpec:
    """«Один человек — разные миры/эпохи»: плавнее, с подписью-меткой к каждому кадру."""
    if len(images) < 3:
        raise ValueError("нужно минимум 3 фото")
    segs = []
    motions = ["zoom_in", "pan_up", "zoom_out", "pan_down"]
    for i, img in enumerate(images):
        overlays = []
        if i == 0:
            overlays += _hook(script.hook, script.style)
        cap = _caption_at(script, i)
        if cap:
            label = tx.render_text(_plain(cap), tx.BADGE)
            overlays.append(Overlay(label, y=SAFE_BOTTOM - 140, start=0.2, anim="pop"))
        segs.append(Segment(
            image=img,
            duration=2.2 if i == 0 else 1.5,
            motion=motions[i % len(motions)],
            background="top_band" if i == 0 else "photo",
            transition="cut" if i == 0 else "crossfade",
            transition_dur=0.35,
            overlays=overlays,
        ))
    segs.append(_cta(images[-1], script))
    return VideoSpec(segs, music=pick_music(music_seed), cover_time=0.8)


FORMATS = {
    "before_after": before_after,
    "looks_carousel": looks_carousel,
    "worlds": worlds,
}
