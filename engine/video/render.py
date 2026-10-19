"""Сборка вертикального видео 1080x1920 из фото: движение камеры, переходы,
анимированные текстовые плашки, музыка. Кадры рисуются в Pillow и потоком
отдаются в ffmpeg, поэтому зум и панорама идут плавно (субпиксельно),
без «дрожания», которое даёт стандартный ffmpeg zoompan.
"""
from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

W, H = 1080, 1920
FPS = 30
ZMAX = 1.15  # запас картинки под зум и панораму

# Безопасная зона Reels/Shorts: сверху шапка, снизу подпись и кнопки,
# справа иконки лайков. Текст держим внутри этих границ.
SAFE_TOP = 250
SAFE_BOTTOM = H - 420
SAFE_RIGHT = W - 120

# Макет «полоса сверху + фото»: доля высоты полотна под заголовок.
BAND_FRAC = 0.36
BAND_COLOR = (14, 14, 18)


def band_text_y(text_height: int) -> int:
    """Верх плашки, чтобы текст встал по центру видимой части полосы."""
    band_bottom = int(H * BAND_FRAC)
    center = (SAFE_TOP - 40 + band_bottom) // 2
    return max(SAFE_TOP - 40, center - text_height // 2)


@dataclass
class Overlay:
    image: Image.Image          # RGBA
    y: int                      # верхняя граница плашки, px
    start: float = 0.0          # относительно начала сегмента, с
    end: float | None = None    # None = до конца сегмента
    anim: str = "pop"           # pop | fade | slide_up | none
    x: int | None = None        # None = по центру безопасной зоны


@dataclass
class Segment:
    image: Image.Image | str | Path
    duration: float
    motion: str = "zoom_in"     # zoom_in | zoom_out | pan_up | pan_down | punch | still
    transition: str = "cut"     # cut | crossfade | flash | slide
    transition_dur: float = 0.3
    overlays: list[Overlay] = field(default_factory=list)
    background: str = "photo"   # photo | top_band (кадр с заголовком) | blur_dark (CTA)

    def __post_init__(self):
        # Полоса должна стоять на месте, иначе текст «уплывёт» с неё.
        if self.background == "top_band":
            self.motion = "still"


@dataclass
class VideoSpec:
    segments: list[Segment]
    music: str | Path | None = None
    music_volume: float = 0.8
    cover_time: float = 0.7     # момент кадра для обложки


# ---------------------------------------------------------------- подготовка

def _load(img: Image.Image | str | Path) -> Image.Image:
    if isinstance(img, Image.Image):
        return ImageOps.exif_transpose(img).convert("RGB")
    with Image.open(img) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


def _prepare(seg: Segment) -> Image.Image:
    """Готовит полотно размером ZMAX*(W,H), по которому ходит «камера»."""
    src = _load(seg.image)
    pw, ph = int(W * ZMAX), int(H * ZMAX)
    sw, sh = src.size

    if seg.background == "top_band":
        # Тёмная полоса сверху под заголовок, фото — под ней, лицо не перекрыто.
        canvas = Image.new("RGB", (pw, ph), BAND_COLOR)
        band_h = int(ph * BAND_FRAC)
        photo = ImageOps.fit(src, (pw, ph - band_h), Image.LANCZOS, centering=(0.5, 0.3))
        canvas.paste(photo, (0, band_h))
        return canvas

    if seg.background == "blur_dark":
        bg = ImageOps.fit(src, (pw, ph), Image.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(40))
        return ImageEnhance.Brightness(bg).enhance(0.45)

    # Горизонтальные и квадратные кадры не режем по бокам: кладём целиком
    # на размытый фон из того же фото. Портретные — заполняют кадр.
    if sw / sh > 0.78:
        bg = ImageOps.fit(src, (pw, ph), Image.LANCZOS).filter(ImageFilter.GaussianBlur(45))
        bg = ImageEnhance.Brightness(bg).enhance(0.6)
        fg = ImageOps.contain(src, (pw, int(ph * 0.72)), Image.LANCZOS)
        bg.paste(fg, ((pw - fg.width) // 2, (ph - fg.height) // 2))
        return bg

    # Портрет: центр кадра чуть выше середины — там обычно лицо.
    return ImageOps.fit(src, (pw, ph), Image.LANCZOS, centering=(0.5, 0.42))


def _ease(p: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, p)))


def _camera(motion: str, t: float, dur: float) -> tuple[float, float, float]:
    """Возвращает (зум, сдвиг_x, сдвиг_y); сдвиги в долях свободного хода, -1..1."""
    p = _ease(t / dur) if dur > 0 else 1.0
    if motion == "zoom_in":
        return 1.0 + (ZMAX - 1.0) * 0.85 * p, 0.0, -0.15 * p
    if motion == "zoom_out":
        return ZMAX - (ZMAX - 1.0) * 0.85 * p, 0.0, -0.15 * (1 - p)
    if motion == "pan_up":
        return ZMAX * 0.97, 0.0, 0.8 - 1.6 * p
    if motion == "pan_down":
        return ZMAX * 0.97, 0.0, -0.8 + 1.6 * p
    if motion == "punch":
        k = min(1.0, t / 0.35)
        z = ZMAX - (ZMAX - 1.02) * (1 - (1 - k) ** 3)
        return z, 0.0, 0.0
    return 1.0 + 0.03 * p, 0.0, 0.0  # still: еле заметный дрейф


def _frame_from(prepared: Image.Image, motion: str, t: float, dur: float) -> Image.Image:
    z, ox, oy = _camera(motion, t, dur)
    z = max(1.0, min(ZMAX, z))
    pw, ph = prepared.size
    a = ZMAX / z                       # масштаб «экран -> полотно»
    win_w, win_h = W * a, H * a
    free_x, free_y = (pw - win_w) / 2, (ph - win_h) / 2
    cx = pw / 2 + ox * free_x
    cy = ph / 2 + oy * free_y
    c = cx - win_w / 2
    f = cy - win_h / 2
    return prepared.transform((W, H), Image.AFFINE, (a, 0, c, 0, a, f), resample=Image.BICUBIC)


def _overlay_alpha_scale(ov: Overlay, t: float, seg_dur: float) -> tuple[float, float, float]:
    """(непрозрачность, масштаб, сдвиг_y) плашки в момент t сегмента."""
    end = seg_dur if ov.end is None else min(ov.end, seg_dur)
    if t < ov.start or t > end:
        return 0.0, 1.0, 0.0
    local = t - ov.start
    out = min(1.0, (end - t) / 0.15) if ov.end is not None else 1.0
    if ov.anim == "pop":
        k = min(1.0, local / 0.28)
        # пружинка: 0.6 -> 1.06 -> 1.0
        scale = 0.6 + 0.46 * math.sin(k * math.pi * 0.62) / math.sin(math.pi * 0.62)
        if k >= 1.0:
            scale = 1.0
        return min(1.0, local / 0.12) * out, scale, 0.0
    if ov.anim == "fade":
        return min(1.0, local / 0.2) * out, 1.0, 0.0
    if ov.anim == "slide_up":
        k = _ease(min(1.0, local / 0.3))
        return k * out, 1.0, (1 - k) * 60
    return out, 1.0, 0.0


def _draw_overlays(frame: Image.Image, seg: Segment, t: float) -> Image.Image:
    if not seg.overlays:
        return frame
    base = frame.convert("RGBA")
    for ov in seg.overlays:
        alpha, scale, dy = _overlay_alpha_scale(ov, t, seg.duration)
        if alpha <= 0.01:
            continue
        img = ov.image
        if abs(scale - 1.0) > 0.005:
            img = img.resize(
                (max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.BICUBIC
            )
        if alpha < 0.999:
            a = img.getchannel("A").point(lambda v, k=alpha: int(v * k))
            img = img.copy()
            img.putalpha(a)
        if ov.x is None:
            # В верхней части кадра центрируем по экрану, внизу — смещаем
            # левее, подальше от колонки иконок справа.
            center = W // 2 if ov.y < 1100 else SAFE_RIGHT // 2 + 20
            x = max(30, center - img.width // 2)
        else:
            x = ov.x - (img.width - ov.image.width) // 2
        y = int(ov.y - (img.height - ov.image.height) / 2 + dy)
        base.alpha_composite(img, (int(x), y))
    return base.convert("RGB")


# ---------------------------------------------------------------- таймлайн

@dataclass
class _Placed:
    seg: Segment
    prepared: Image.Image
    start: float
    end: float


def _place(spec: VideoSpec) -> tuple[list[_Placed], float]:
    placed: list[_Placed] = []
    t = 0.0
    for i, seg in enumerate(spec.segments):
        overlap = seg.transition_dur if (i > 0 and seg.transition in ("crossfade", "slide")) else 0.0
        start = max(0.0, t - overlap)
        end = start + seg.duration
        placed.append(_Placed(seg, _prepare(seg), start, end))
        t = end
    return placed, t


def render_frame(placed: list[_Placed], t: float) -> Image.Image:
    active = [p for p in placed if p.start <= t < p.end] or [placed[-1]]
    cur = active[-1]
    frame = _draw_overlays(
        _frame_from(cur.prepared, cur.seg.motion, t - cur.start, cur.seg.duration),
        cur.seg, t - cur.start,
    )
    if len(active) > 1:
        prev = active[-2]
        prev_frame = _draw_overlays(
            _frame_from(prev.prepared, prev.seg.motion, t - prev.start, prev.seg.duration),
            prev.seg, t - prev.start,
        )
        k = _ease((t - cur.start) / max(1e-6, cur.seg.transition_dur))
        if cur.seg.transition == "slide":
            canvas = Image.new("RGB", (W, H))
            off = int(W * (1 - k))
            canvas.paste(prev_frame, (off - W, 0))
            canvas.paste(frame, (off, 0))
            frame = canvas
        else:
            frame = Image.blend(prev_frame, frame, k)
    if cur.seg.transition == "flash":
        since = t - cur.start
        if since < 0.25:
            k = 1 - since / 0.25
            frame = Image.blend(frame, Image.new("RGB", (W, H), (255, 255, 255)), 0.85 * k)
    return frame


def render(spec: VideoSpec, out_path: str | Path, cover_path: str | Path | None = None,
           crf: int = 20, preset: str = "medium") -> float:
    """Рендерит видео в out_path (mp4). Возвращает длительность в секундах."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg не найден в системе")
    if not spec.segments:
        raise ValueError("пустой список сегментов")

    placed, total = _place(spec)
    n_frames = int(round(total * FPS))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
    ]
    if spec.music:
        cmd += ["-stream_loop", "-1", "-i", str(spec.music)]
        fade_out = max(0.0, total - 0.8)
        afilter = (f"[1:a]volume={spec.music_volume},afade=t=in:d=0.3,"
                   f"afade=t=out:st={fade_out:.2f}:d=0.8[a]")
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        afilter = "[1:a]anull[a]"
    cmd += [
        "-filter_complex", afilter, "-map", "0:v", "-map", "[a]",
        "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-g", str(FPS * 2), "-r", str(FPS),
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-movflags", "+faststart",
        str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    cover_saved = False
    try:
        for i in range(n_frames):
            t = i / FPS
            frame = render_frame(placed, t)
            if cover_path and not cover_saved and t >= spec.cover_time:
                frame.save(cover_path, "JPEG", quality=90)
                cover_saved = True
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    proc.stderr.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg завершился с ошибкой: {err.strip()}")
    if cover_path and not cover_saved:
        render_frame(placed, 0).save(cover_path, "JPEG", quality=90)
    return total
