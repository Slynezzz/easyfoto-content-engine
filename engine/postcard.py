"""Карточка поста 4:5 (1080×1350): картинка, заголовок с выделенными словами и подзаголовок.

Формат 4:5 одинаково хорошо смотрится в ленте Instagram, в Telegram-канале и во VK.
Заголовок лежит в нижней части на мягком затемнении, слова в *звёздочках* — цветом акцента;
вверху — маленькая плашка автора. Шрифт Inter, как в роликах.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .video.text import FONT_BOLD, FONT_SEMI

W, H = 1080, 1350
MARGIN = 72
ACCENT = (94, 234, 212)        # бирюзовый «у моря»
WHITE = (255, 255, 255)
SUB = (226, 232, 240)
SHORT = {"в", "во", "к", "ко", "с", "со", "и", "а", "о", "об", "у", "на", "по", "за", "до", "из", "от", "не",
         "но", "ни", "да", "для", "без", "при", "про"}

ILLUSTRATION_PROMPT = """Создай одно изображение для поста в соцсетях (формат ленты 4:5, вертикальное).
Требования:
- Высокое качество, современная эстетика: реалистичная фотография или аккуратная редакционная иллюстрация — как указано в стиле.
- Никакого текста, букв, цифр, логотипов, водяных знаков и интерфейсов с читаемыми надписями.
- Без узнаваемых лиц реальных людей и знаменитостей; люди — со спины, в профиль, размыто или вдалеке.
- Один ясный главный образ в верхней и средней части кадра, который считывается за секунду на экране телефона.
- Нижняя треть кадра спокойная, тёмная или однородная, без мелких деталей: туда ляжет крупный заголовок."""


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def _fit_cover(im: Image.Image, author_photo: bool) -> Image.Image:
    """Обрезка под 4:5 без искажений. Для фото автора — держим верх (лицо), иначе центр."""
    im = im.convert("RGB")
    scale = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    x = (im.width - W) // 2
    y = int((im.height - H) * (0.15 if author_photo else 0.5))
    return im.crop((x, y, x + W, y + H))


def _tokens(text: str) -> list[tuple[str, bool]]:
    """«Вы *теряете* заявки» → [(Вы, False), (теряете, True), (заявки, False)]."""
    out, accent = [], False
    for i, part in enumerate(text.split("*")):
        accent = i % 2 == 1
        out += [(w, accent) for w in part.split()]
    # короткие предлоги и союзы не висят в конце строки: «в 9:15», «и как» — одним словом
    glued: list[tuple[str, bool]] = []
    for w, a in out:
        if glued and glued[-1][0].lower() in SHORT and glued[-1][1] == a:
            prev, _ = glued.pop()
            glued.append((prev + "\u00a0" + w, a))
        else:
            glued.append((w, a))
    return glued


def _wrap(tokens, font, width) -> list[list[tuple[str, bool]]]:
    lines, cur = [], []
    for tok in tokens:
        trial = " ".join(w for w, _ in cur + [tok])
        if cur and font.getlength(trial) > width:
            lines.append(cur)
            cur = [tok]
        else:
            cur.append(tok)
    if cur:
        lines.append(cur)
    return lines


def _headline_layout(text: str, width: int, max_lines: int = 4):
    toks = _tokens(text)
    for size in range(92, 47, -4):
        font = _font(FONT_BOLD, size)
        lines = _wrap(toks, font, width)
        if len(lines) <= max_lines and all(font.getlength(" ".join(w for w, _ in ln)) <= width for ln in lines):
            return font, lines
    font = _font(FONT_BOLD, 48)
    return font, _wrap(toks, font, width)[:max_lines]


def compose(image: Image.Image, headline: str, subline: str, out: Path, brand: str = "",
            author_photo: bool = False) -> Path:
    base = _fit_cover(image, author_photo)
    width = W - 2 * MARGIN
    font, lines = _headline_layout(headline or "", width)
    sub_font = _font(FONT_SEMI, 38)
    sub_lines = _wrap([(w, False) for w in (subline or "").split()], sub_font, width)[:2]

    ascent, descent = font.getmetrics()
    line_h = int((ascent + descent) * 1.02)
    sub_h = int(sum(sub_font.getmetrics()) * 1.15)
    block = line_h * len(lines) + (24 + sub_h * len(sub_lines) if sub_lines else 0)
    top = H - MARGIN - block

    # затемнение снизу: от прозрачного к тёмному под текстом
    shade = Image.new("L", (1, H), 0)
    start = max(0, top - 260)
    for y in range(H):
        a = 0 if y < start else min(225, int(225 * ((y - start) / max(1, H - start)) ** 0.8))
        shade.putpixel((0, y), a)
    dark = Image.new("RGB", (W, H), (8, 15, 26))
    base = Image.composite(dark, base, shade.resize((W, H)))

    d = ImageDraw.Draw(base)
    y = top
    space = font.getlength(" ")
    for ln in lines:
        x = MARGIN
        for w, accent in ln:
            d.text((x, y), w, font=font, fill=ACCENT if accent else WHITE)
            x += font.getlength(w) + space
        y += line_h
    if sub_lines:
        y += 24
        for ln in sub_lines:
            d.text((MARGIN, y), " ".join(w for w, _ in ln), font=sub_font, fill=SUB)
            y += sub_h

    if brand:
        bf = _font(FONT_SEMI, 30)
        tw = bf.getlength(brand)
        pad_x, pad_y = 22, 12
        box = (MARGIN - 8, MARGIN - 8, MARGIN - 8 + tw + 2 * pad_x, MARGIN - 8 + 30 + 2 * pad_y + 6)
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(box, radius=26, fill=(8, 15, 26, 170))
        base = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
        ImageDraw.Draw(base).text((box[0] + pad_x, box[1] + pad_y), brand, font=bf, fill=WHITE)

    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out, "JPEG", quality=92)
    return out
