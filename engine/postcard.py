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
    # короткие предлоги и союзы не висят в конце строки: слово, оканчивающееся неразрывным
    # пробелом, переносится только вместе со следующим (цвет у каждого свой)
    return [(w + NBSP, a) if i + 1 < len(out) and w.lower() in SHORT else (w, a) for i, (w, a) in enumerate(out)]


NBSP = "\u00a0"


def _line_width(tokens, font) -> float:
    space = font.getlength(" ")
    return sum(font.getlength(w) + (0 if w.endswith(NBSP) else space) for w, _ in tokens) - (space if tokens else 0)


def _wrap(tokens, font, width) -> list[list[tuple[str, bool]]]:
    units, cur = [], []
    for tok in tokens:  # «на» + «каждом» — одна неразрывная единица
        cur.append(tok)
        if not tok[0].endswith(NBSP):
            units.append(cur)
            cur = []
    if cur:
        units.append(cur)
    lines, line = [], []
    for u in units:
        if line and _line_width(line + u, font) > width:
            lines.append(line)
            line = list(u)
        else:
            line += u
    if line:
        lines.append(line)
    return lines


def _advance(font, w: str) -> float:
    return font.getlength(w) + (0 if w.endswith(NBSP) else font.getlength(" "))


def _headline_layout(text: str, width: int, max_lines: int = 4):
    toks = _tokens(text)
    for size in range(92, 47, -4):
        font = _font(FONT_BOLD, size)
        lines = _wrap(toks, font, width)
        if len(lines) <= max_lines and all(_line_width(ln, font) <= width for ln in lines):
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
            x += _advance(font, w)
        y += line_h
    if sub_lines:
        y += 24
        for ln in sub_lines:
            d.text((MARGIN, y), "".join(w if w.endswith(NBSP) else w + " " for w, _ in ln).strip(), font=sub_font,
                   fill=SUB)
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


# ------------------------------------------------------------------ слайды карусели
NAVY = (15, 27, 45)
MUTED = (148, 163, 184)


def _background() -> Image.Image:
    """Тёмно-синий фон с мягким бирюзовым свечением в углу — единый стиль всех слайдов."""
    base = Image.new("RGB", (W, H), NAVY)
    glow = Image.new("L", (W, H), 0)
    gd = ImageDraw.Draw(glow)
    for r in range(700, 0, -20):
        gd.ellipse((W - r, -r, W + r, r), fill=int(70 * (1 - r / 700)))
    return Image.composite(Image.new("RGB", (W, H), (20, 184, 166)), base, glow)


def _draw_rich(d: ImageDraw.ImageDraw, x: int, y: int, text: str, font, width: int, color=WHITE,
               line_k: float = 1.12, max_lines: int = 99) -> int:
    lines = _wrap(_tokens(text), font, width)[:max_lines]
    a, ds = font.getmetrics()
    lh = int((a + ds) * line_k)
    space = font.getlength(" ")
    for ln in lines:
        cx = x
        for w, accent in ln:
            d.text((cx, y), w, font=font, fill=ACCENT if accent else color)
            cx += _advance(font, w)
        y += lh
    return y


def slide(title: str, text: str, out: Path, index: int, total: int, brand: str = "", source: str = "",
          last: bool = False) -> Path:
    """Слайд с мыслью: номер, заголовок с акцентом, текст, источник цифры, подвал с автором."""
    im = _background()
    d = ImageDraw.Draw(im)
    width = W - 2 * MARGIN
    d.text((MARGIN, MARGIN), f"{index:02d} / {total:02d}", font=_font(FONT_SEMI, 30), fill=ACCENT)
    y = MARGIN + 110
    for size in (76, 68, 60, 54):
        tf = _font(FONT_BOLD, size)
        if len(_wrap(_tokens(title), tf, width)) <= 3:
            break
    y = _draw_rich(d, MARGIN, y, title, tf, width, max_lines=4) + 40
    d.rectangle((MARGIN, y, MARGIN + 90, y + 6), fill=ACCENT)
    y += 50
    body_lines = None
    for size in (44, 40, 36, 33):
        bf = _font(FONT_SEMI, size)
        body_lines = _wrap(_tokens(text), bf, width)
        if y + len(body_lines) * int(sum(bf.getmetrics()) * 1.3) < H - 230:
            break
    _draw_rich(d, MARGIN, y, text, bf, width, color=SUB, line_k=1.3, max_lines=12)
    foot_y = H - MARGIN - 34
    if source:
        sf = _font(FONT_SEMI, 26)
        src = f"Источник: {source}"
        while sf.getlength(src) > width and len(src) > 20:
            src = src[:-2].rstrip() + "…"
        d.text((MARGIN, foot_y - 56), src, font=sf, fill=MUTED)
    ff = _font(FONT_SEMI, 28)
    if brand:
        d.text((MARGIN, foot_y), brand, font=ff, fill=MUTED)
    if not last:
        hint = "листайте →"
        d.text((W - MARGIN - ff.getlength(hint), foot_y), hint, font=ff, fill=ACCENT)
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out, "JPEG", quality=92)
    return out
