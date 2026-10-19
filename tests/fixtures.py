"""Тестовые фото-заглушки: градиент + силуэт, чтобы видеть движение и кадрирование."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

PALETTES = [((40, 20, 80), (230, 120, 60)), ((10, 60, 90), (120, 220, 200)),
            ((90, 10, 30), (250, 200, 120)), ((20, 20, 20), (180, 180, 200)),
            ((30, 70, 30), (220, 230, 140)), ((60, 30, 10), (240, 170, 200))]

def make(path: Path, size=(1024, 1536), idx=0, label="") -> Path:
    w, h = size
    c1, c2 = PALETTES[idx % len(PALETTES)]
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(h):
        k = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(c1[i] * (1 - k) + c2[i] * k) for i in range(3)))
    cx = w // 2
    d.ellipse([cx - w*0.16, h*0.22, cx + w*0.16, h*0.22 + w*0.4], fill=(240, 210, 190))
    d.rounded_rectangle([cx - w*0.3, h*0.22 + w*0.42, cx + w*0.3, h], radius=80, fill=(30, 30, 40))
    for gx in range(0, w, 128):
        d.line([(gx, 0), (gx, h)], fill=(255, 255, 255), width=1)
    f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 60)
    d.text((40, h - 120), label, font=f, fill=(255, 255, 255))
    img.save(path, quality=92)
    return path
