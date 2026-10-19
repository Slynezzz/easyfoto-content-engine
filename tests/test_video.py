import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.video import text as tx
from engine.video.render import H, W, Overlay, Segment, VideoSpec, render
from engine.video.templates import FORMATS, Script
from tests.fixtures import make


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,pix_fmt",
         "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


class VideoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.before = make(self.tmp / "before.jpg", (1080, 1440), 3, "до")
        self.afters = [make(self.tmp / f"a{i}.jpg", (1024, 1024) if i == 1 else (1024, 1536), i)
                       for i in range(3)]

    def tearDown(self):
        self._tmp.cleanup()

    def test_text_wraps_and_fits_width(self):
        img = tx.render_text("Очень длинный заголовок, который точно не влезет в одну строку", tx.HOOK)
        self.assertEqual(img.mode, "RGBA")
        self.assertLessEqual(img.width, tx.HOOK.max_width + 2 * tx.HOOK.stroke)
        self.assertGreater(img.height, 200)

    def test_text_shrinks_long_word(self):
        img = tx.render_text("Суперкалифрагилистикэкспиалидоциусище", tx.HOOK)
        self.assertLessEqual(img.width, tx.HOOK.max_width + 2 * tx.HOOK.stroke)

    def test_render_produces_instagram_compatible_mp4(self):
        spec = VideoSpec([
            Segment(self.afters[0], 0.6, background="top_band",
                    overlays=[Overlay(tx.render_text("Хук", tx.HOOK), y=400)]),
            Segment(self.afters[1], 0.6, motion="zoom_in", transition="crossfade", transition_dur=0.2),
            Segment(self.afters[2], 0.6, motion="punch", transition="flash"),
        ])
        out, cover = self.tmp / "v.mp4", self.tmp / "c.jpg"
        dur = render(spec, out, cover_path=cover, preset="ultrafast")
        info = _probe(out)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next(s for s in info["streams"] if s["codec_type"] == "audio")
        self.assertEqual((v["codec_name"], v["width"], v["height"], v["pix_fmt"]), ("h264", W, H, "yuv420p"))
        self.assertEqual(a["codec_name"], "aac")
        self.assertLess(abs(float(info["format"]["duration"]) - dur), 0.15)
        self.assertTrue(cover.exists() and cover.stat().st_size > 0)

    def test_top_band_forces_static_camera(self):
        self.assertEqual(Segment("x.jpg", 1.0, motion="punch", background="top_band").motion, "still")

    def test_formats_build_valid_specs(self):
        script = Script(hook="Хук", captions=["Сцена 1", "Сцена 2", "Сцена 3"])
        for name, fn in FORMATS.items():
            with self.subTest(name=name):
                spec = fn(self.before, self.afters, script) if name == "before_after" else fn(self.afters, script)
                self.assertEqual(spec.segments[-1].background, "blur_dark")
                total = sum(s.duration for s in spec.segments)
                self.assertTrue(5 < total < 30)

    def test_formats_reject_too_few_photos(self):
        with self.assertRaises(ValueError):
            FORMATS["looks_carousel"](self.afters[:2], Script(hook="x"))


if __name__ == "__main__":
    unittest.main()
