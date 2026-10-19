"""Стили текста на видео («скины»). Система сама тестирует их, как форматы и хуки.

Разметка: слово или фраза в *звёздочках* — ключевое, оно выделяется
по правилам стиля (цветом или плашкой). Без звёздочек текст обычный.

Раскладка идёт по словам, поэтому можно:
  - рисовать плашку под каждой строкой (стиль TikTok-субтитров);
  - выделять одно слово цветной плашкой;
  - показывать слова по одному, не сдвигая уже показанные (reveal).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter

from .text import FONT_BOLD, FONT_SEMI, _font

RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class Skin:
    name: str
    about: str
    color: RGBA = (255, 255, 255, 255)
    stroke: int = 7
    stroke_color: RGBA = (0, 0, 0, 230)
    bold: bool = True
    line_box: RGBA | None = None      # плашка под каждой строкой
    accent_color: RGBA = (255, 214, 10, 255)
    accent_box: RGBA | None = None    # плашка под ключевым словом
    accent_text: RGBA | None = None   # цвет ключевого слова на плашке
    case: str = "as_is"               # as_is | lower | upper
    shadow: bool = False
    size_mult: float = 1.0
    kinetic: bool = False             # хук появляется по словам


SKINS: dict[str, Skin] = {
    "clean": Skin("clean", "белый жирный текст с обводкой, ключевое слово жёлтым"),
    "boxed": Skin("boxed", "чёрный текст на белых плашках по строкам, как субтитры TikTok",
                  color=(18, 18, 18, 255), stroke=0, line_box=(255, 255, 255, 245),
                  accent_color=(230, 40, 70, 255), size_mult=0.92),
    "highlight": Skin("highlight", "белый текст, ключевое слово на жёлтой плашке",
                      stroke=6, accent_box=(255, 214, 10, 255), accent_text=(15, 15, 15, 255)),
    "kinetic": Skin("kinetic", "хук появляется по одному слову, ключевое слово жёлтым", kinetic=True),
    "soft": Skin("soft", "строчные буквы, без обводки, мягкая тень — эстетичный минимализм",
                 stroke=0, bold=False, case="lower", shadow=True,
                 accent_color=(255, 190, 210, 255), size_mult=0.95),
}
DEFAULT_SKIN = "clean"

_MARK = re.compile(r"\*([^*]+)\*")


def get(name: str | None) -> Skin:
    return SKINS.get(name or DEFAULT_SKIN, SKINS[DEFAULT_SKIN])


def strip_marks(text: str) -> str:
    return _MARK.sub(r"\1", text).replace("*", "")


def tokens(text: str) -> list[tuple[str, bool]]:
    """Слова с флагом «ключевое». *две части* выделяются целиком."""
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in _MARK.finditer(text):
        out += [(w, False) for w in text[pos:m.start()].replace("*", "").split()]
        out += [(w, True) for w in m.group(1).split()]
        pos = m.end()
    out += [(w, False) for w in text[pos:].replace("*", "").split()]
    return out


def word_count(text: str) -> int:
    return len(tokens(text))


def _case(word: str, skin: Skin) -> str:
    return word.lower() if skin.case == "lower" else word.upper() if skin.case == "upper" else word


def _layout(words: list[tuple[str, bool]], font, avail: float, space: float,
            extra: float = 0.0) -> list[list[int]]:
    lines: list[list[int]] = [[]]
    width = 0.0
    for i, (w, acc) in enumerate(words):
        wl = font.getlength(w) + (2 * extra if acc else 0)
        if lines[-1] and width + space + wl > avail:
            if w in ("—", "–", "-") and len(lines[-1]) > 1:
                # тире не начинает строку: переносим вместе с предыдущим словом
                prev = lines[-1].pop()
                lines.append([prev, i])
                width = font.getlength(words[prev][0]) + space + wl
            elif w in ("—", "–", "-"):
                # в строке одно слово — оставляем тире при нём, даже чуть шире
                lines[-1].append(i)
                width += space + wl
            else:
                lines.append([i])
                width = wl
        else:
            width += (space if lines[-1] else 0) + wl
            lines[-1].append(i)
    return [ln for ln in lines if ln]


def render(text: str, skin: Skin | str, size: int, max_width: int = 900,
           reveal: int | None = None, min_size: int = 40) -> Image.Image:
    """Рисует текст в стиле skin. reveal=N — показаны только первые N слов
    (раскладка как у полного текста, чтобы слова не прыгали)."""
    if isinstance(skin, str):
        skin = get(skin)
    words = [(_case(w, skin), acc) for w, acc in tokens(text)] or [("", False)]
    size = int(size * skin.size_mult)
    font_path = str(FONT_BOLD if skin.bold else FONT_SEMI)

    line_pad_x = 22 if skin.line_box else 0
    acc_pad = 12 if skin.accent_box else 0
    margin = skin.stroke + line_pad_x + acc_pad + (12 if skin.shadow else 0)
    avail = max_width - 2 * margin
    while True:
        font = _font(font_path, size)
        if max(font.getlength(w) for w, _ in words) <= avail or size <= min_size:
            break
        size -= 4
    space = font.getlength(" ")
    lines = _layout(words, font, avail, space, acc_pad)

    def place(ln: list[int]) -> tuple[list[float], float]:
        """Смещения слов от начала строки и её ширина. Поля плашки — только
        по краям выделенной фразы, внутри неё обычный пробел."""
        pos, cx = [], 0.0
        for k, i in enumerate(ln):
            acc = words[i][1]
            if acc and (k == 0 or not words[ln[k - 1]][1]):
                cx += acc_pad
            pos.append(cx)
            cx += font.getlength(words[i][0])
            if acc and (k == len(ln) - 1 or not words[ln[k + 1]][1]):
                cx += acc_pad
            if k < len(ln) - 1:
                cx += space
        return pos, cx

    ascent, descent = font.getmetrics()
    line_h = ascent + descent
    pad_y = int(line_h * 0.12) if (skin.line_box or skin.accent_box) else 0
    gap = int(line_h * (0.28 if skin.line_box else 0.16))
    placed = [place(ln) for ln in lines]
    widths = [w for _, w in placed]
    content_w = math.ceil(max(widths))
    w_img = content_w + 2 * margin
    h_img = len(lines) * (line_h + 2 * pad_y) + gap * (len(lines) - 1) + 2 * margin

    text_layer = Image.new("RGBA", (w_img, h_img), (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_layer)
    shown = len(words) if reveal is None else max(0, reveal)

    y = margin
    for ln, (offsets, lw) in zip(lines, placed):
        x = margin + (content_w - lw) / 2
        visible = [i for i in ln if i < shown]
        if skin.line_box and visible:
            vis_w = place(visible)[1]
            draw.rounded_rectangle(
                (x - line_pad_x, y, x + vis_w + line_pad_x, y + line_h + 2 * pad_y),
                radius=int(line_h * 0.28), fill=skin.line_box)
        pos = [x + o for o in offsets]
        # плашки ключевых слов: подряд идущие выделенные слова — одной плашкой
        if skin.accent_box:
            k = 0
            while k < len(ln):
                if not (words[ln[k]][1] and ln[k] < shown):
                    k += 1
                    continue
                j = k
                while j + 1 < len(ln) and words[ln[j + 1]][1] and ln[j + 1] < shown:
                    j += 1
                x0 = pos[k] - acc_pad
                x1 = pos[j] + font.getlength(words[ln[j]][0]) + acc_pad
                draw.rounded_rectangle((x0, y + pad_y * 0.3, x1, y + line_h + pad_y * 1.7),
                                       radius=int(line_h * 0.22), fill=skin.accent_box)
                k = j + 1
        for i, px in zip(ln, pos):
            if i >= shown:
                continue
            word, accent = words[i]
            color = skin.color
            if accent:
                color = (skin.accent_text or skin.color) if skin.accent_box else skin.accent_color
            stroke = 0 if (accent and skin.accent_box) else skin.stroke
            draw.text((px, y + pad_y), word, font=font, fill=color,
                      stroke_width=stroke, stroke_fill=skin.stroke_color)
        y += line_h + 2 * pad_y + gap

    if not skin.shadow:
        return text_layer
    shadow = Image.new("RGBA", text_layer.size, (0, 0, 0, 0))
    shadow.putalpha(text_layer.getchannel("A").point(lambda v: int(v * 0.75)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    out = Image.new("RGBA", text_layer.size, (0, 0, 0, 0))
    out.alpha_composite(shadow, (0, 4))
    out.alpha_composite(text_layer)
    return out
