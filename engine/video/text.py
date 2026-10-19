"""Рендер текстовых плашек (хуки, подписи, бейджи, CTA) в RGBA-картинки.

Текст рисуем Pillow, а не ffmpeg drawtext: так проще перенос строк,
обводка, плашки и анимация появления, и кириллица гарантированно
выглядит одинаково локально и на Railway (шрифт лежит в репозитории).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
FONT_BOLD = FONTS_DIR / "Inter-ExtraBold.otf"
FONT_SEMI = FONTS_DIR / "Inter-SemiBold.otf"


@lru_cache(maxsize=64)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


@dataclass(frozen=True)
class TextStyle:
    size: int = 84
    bold: bool = True
    color: tuple[int, int, int, int] = (255, 255, 255, 255)
    stroke: int = 6
    stroke_color: tuple[int, int, int, int] = (0, 0, 0, 230)
    box: tuple[int, int, int, int] | None = None  # фон-плашка RGBA
    box_pad: int = 28
    box_radius: int = 26
    line_gap: float = 0.18
    max_width: int = 900
    align: str = "center"
    uppercase: bool = False


def _split_long_word(word: str, font: ImageFont.FreeTypeFont, avail: float) -> list[str]:
    """Режет слово по буквам с дефисом, если оно шире строки."""
    parts, cur = [], ""
    for ch in word:
        if cur and font.getlength(cur + ch + "-") > avail:
            parts.append(cur + "-")
            cur = ch
        else:
            cur += ch
    parts.append(cur)
    return parts


def _wrap(text: str, font: ImageFont.FreeTypeFont, avail: float) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words: list[str] = []
        for w in paragraph.split():
            words.extend(_split_long_word(w, font, avail) if font.getlength(w) > avail else [w])
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            trial = f"{current} {word}"
            if not current.endswith("-") and font.getlength(trial) <= avail:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def render_text(text: str, style: TextStyle, min_size: int = 40) -> Image.Image:
    """Возвращает RGBA-картинку с текстом не шире style.max_width.

    Сначала пробует уменьшить шрифт так, чтобы слова не рвались;
    если не помогает даже min_size — режет длинное слово по буквам.
    """
    if style.uppercase:
        text = text.upper()
    font_path = str(FONT_BOLD if style.bold else FONT_SEMI)
    pad = style.box_pad if style.box else 0
    avail = style.max_width - 2 * pad - 2 * style.stroke
    size = style.size
    while True:
        font = _font(font_path, size)
        longest_word = max((font.getlength(w) for w in text.split()), default=0)
        if longest_word <= avail or size <= min_size:
            break
        size -= 4
    lines = _wrap(text, font, avail)
    text_w = math.ceil(max((font.getlength(line) for line in lines), default=0))

    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    gap = int(line_h * style.line_gap)
    text_h = line_h * len(lines) + gap * (len(lines) - 1)

    canvas_w = text_w + 2 * pad + 2 * style.stroke
    canvas_h = text_h + 2 * pad + 2 * style.stroke
    img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if style.box:
        draw.rounded_rectangle(
            (0, 0, canvas_w - 1, canvas_h - 1), radius=style.box_radius, fill=style.box
        )

    y = pad + style.stroke
    for line in lines:
        lw = font.getlength(line)
        if style.align == "center":
            x = (canvas_w - lw) / 2
        elif style.align == "right":
            x = canvas_w - pad - style.stroke - lw
        else:
            x = pad + style.stroke
        draw.text(
            (x, y),
            line,
            font=font,
            fill=style.color,
            stroke_width=style.stroke,
            stroke_fill=style.stroke_color,
        )
        y += line_h + gap
    return img


# Готовые стили под форматы рилс -------------------------------------------

HOOK = TextStyle(size=92, stroke=7, max_width=900)
HOOK_BOXED = TextStyle(
    size=84, stroke=0, color=(20, 20, 20, 255), box=(255, 255, 255, 240), max_width=880
)
CAPTION = TextStyle(size=58, stroke=5, max_width=860)
BADGE = TextStyle(
    size=56, stroke=0, color=(255, 255, 255, 255), box=(230, 40, 70, 235),
    box_pad=18, box_radius=18, uppercase=True, max_width=600,
)
CTA_TITLE = TextStyle(size=96, stroke=0, max_width=900)
CTA_SUB = TextStyle(
    size=52, bold=False, stroke=0, color=(20, 20, 20, 255),
    box=(255, 214, 10, 245), box_pad=26, box_radius=24, max_width=880,
)
