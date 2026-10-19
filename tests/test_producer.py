import io
import json
import random
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from engine import images, playbook as pbm
from engine.config import Config
from engine.db import DB
from engine.llm import AIError, ImageResult
from engine.producer import ProduceError, produce
from tests.fixtures import make

SCRIPT = {
    "hook": "Скинул боту селфи — вот результат",
    "captions": ["a", "b", "c", "d", "e", "f"],
    "cta_title": "Хочешь так же?",
    "cta_sub": "Бот — по ссылке в профиле",
    "post_caption": "Все кадры сделаны ботом из обычных селфи. Какой образ твой? " * 2,
    "hashtags": ["#нейрофото"],
}


class FakeAI:
    """Отвечает как Gemini: разметка фото, сценарии, картинки."""

    def __init__(self, image_bytes: bytes | None = None, refuse_every: int = 0):
        self.image_bytes = image_bytes
        self.refuse_every = refuse_every
        self.image_calls = 0
        self.json_calls = []

    def json(self, system, prompt, images=None, temperature=0.9):
        self.json_calls.append(prompt)
        if images:  # разметка библиотеки
            return {"title": "Нуар в дождь", "theme": "cinema", "description": "улица, плащ"}
        resp = dict(SCRIPT)
        if '"scenes"' in prompt:
            resp["scenes"] = [{"title": f"Сцена {i}", "where": "город", "wearing": "плащ",
                               "doing": "идёт", "around": "дождь", "light": "неон"} for i in range(6)]
        return resp

    def image(self, prompt, refs):
        self.image_calls += 1
        if self.refuse_every and self.image_calls % self.refuse_every == 0:
            raise AIError("filtered")
        return ImageResult(self.image_bytes, "image/jpeg", "fake")


class ProducerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = Config(data_dir=self.tmp / "data")
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.pb = pbm.load(self.cfg.playbook_path)
        self.today = date(2026, 9, 10)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _fill(self, kind: str, count: int):
        for i in range(count):
            make(self.cfg.assets_dir / kind / f"{kind}{i}.jpg", (1024, 1536), i)

    def test_library_mode_end_to_end(self):
        self._fill("library", 6)
        self._fill("before", 1)
        self.assertEqual(images.scan(self.cfg, self.db), {"library": 6, "before": 1, "refs": 0})
        ai = FakeAI()
        self.assertEqual(images.tag_library(self.cfg, self.db, ai, self.pb["themes"]), 6)
        post_id = produce(self.cfg, self.db, ai, self.pb, self.today, rng=random.Random(3))
        post = self.db.post(post_id)
        self.assertEqual(post["status"], "rendered")
        self.assertTrue(Path(post["video_path"]).stat().st_size > 50_000)
        self.assertTrue(Path(post["cover_path"]).exists())
        self.assertIn("#нейрофото", post["caption"])
        self.assertEqual(post["theme"], "cinema")
        used = self.db.q("SELECT SUM(used_count) s FROM assets")[0]["s"]
        self.assertGreaterEqual(used, 3)
        plan = json.loads(post["script_json"])
        self.assertEqual(plan["choice"]["theme"], "cinema")

    def test_before_after_not_chosen_without_before_photos(self):
        self._fill("library", 6)
        images.scan(self.cfg, self.db)
        ai = FakeAI()
        images.tag_library(self.cfg, self.db, ai, self.pb["themes"])
        with mock.patch("engine.producer.render", return_value=10.0):
            formats = {self.db.post(produce(self.cfg, self.db, ai, self.pb, self.today,
                                            rng=random.Random(i)))["format"] for i in range(15)}
        self.assertNotIn("before_after", formats)

    def test_generate_mode_skips_refused_scenes(self):
        self.cfg.image_source = "generate"
        self._fill("refs", 3)
        images.scan(self.cfg, self.db)
        buf = io.BytesIO()
        from PIL import Image
        Image.new("RGB", (768, 1344), (120, 80, 60)).save(buf, "JPEG")
        ai = FakeAI(buf.getvalue(), refuse_every=3)
        with mock.patch("engine.producer.render", return_value=10.0):
            post_id = produce(self.cfg, self.db, ai, self.pb, self.today, rng=random.Random(1))
        post = self.db.post(post_id)
        plan = json.loads(post["script_json"])
        n_ok = len(json.loads(post["asset_ids_json"]))
        self.assertGreaterEqual(n_ok, 3)
        self.assertEqual(len(plan["script"]["captions"]), n_ok)
        self.assertTrue(all(c.startswith("Сцена") for c in plan["script"]["captions"]))

    def test_corrupt_reference_is_skipped(self):
        self.cfg.image_source = "generate"
        self._fill("refs", 2)
        (self.cfg.assets_dir / "refs" / "broken.jpg").write_bytes(b"not an image")
        images.scan(self.cfg, self.db)
        buf = io.BytesIO()
        from PIL import Image
        Image.new("RGB", (768, 1344)).save(buf, "JPEG")
        ai = FakeAI(buf.getvalue())
        aid = images.generate(self.cfg, self.db, ai, {"title": "x", "where": "y"}, "cinema")
        self.assertTrue(aid)
        self.assertEqual(list((self.cfg.data_dir / "work").glob("*")), [])

    def test_generate_requires_refs(self):
        self.cfg.image_source = "generate"
        ai = FakeAI(b"")
        with mock.patch("engine.producer.render", return_value=10.0):
            with self.assertRaises(ProduceError):
                produce(self.cfg, self.db, ai, self.pb, self.today, rng=random.Random(1))

    def test_empty_library_explains_what_to_do(self):
        with self.assertRaises(ProduceError) as ctx:
            produce(self.cfg, self.db, FakeAI(), self.pb, self.today)
        self.assertIn("библиотек", str(ctx.exception))

    def test_global_tagging_failure_raises_and_does_not_burn_attempts(self):
        self._fill("library", 4)
        images.scan(self.cfg, self.db)

        class Down(FakeAI):
            def json(self, system, prompt, images=None, temperature=0.9):
                raise AIError("API key not valid")
        for _ in range(5):
            with self.assertRaises(images.TaggingError) as ctx:
                images.tag_library(self.cfg, self.db, Down(), self.pb["themes"])
        self.assertIn("API key not valid", str(ctx.exception))
        self.assertTrue(all(r["tag_attempts"] == 0 for r in self.db.assets("library")))

    def test_single_bad_photo_is_skipped_after_three_attempts(self):
        self._fill("library", 3)
        images.scan(self.cfg, self.db)
        bad_id = self.db.assets("library")[1]["id"]

        class OneBad(FakeAI):
            def json(self, system, prompt, images=None, temperature=0.9):
                if images and f"tag_{bad_id}." in str(images[0]):
                    raise AIError("refused")
                return super().json(system, prompt, images, temperature)
        for _ in range(5):
            images.tag_library(self.cfg, self.db, OneBad(), self.pb["themes"])
        rows = {r["id"]: r for r in self.db.assets("library")}
        self.assertEqual(rows[bad_id]["tag_attempts"], 3)
        self.assertEqual(sum(1 for r in rows.values() if r["title"]), 2)

    def test_reel_is_made_even_when_tagging_is_down(self):
        self._fill("library", 6)
        images.scan(self.cfg, self.db)

        class Down(FakeAI):
            def json(self, system, prompt, images=None, temperature=0.9):
                raise AIError("quota exceeded")
        with mock.patch("engine.producer.render", return_value=10.0):
            post_id = produce(self.cfg, self.db, Down(), self.pb, self.today, rng=random.Random(2))
        post = self.db.post(post_id)
        self.assertEqual((post["status"], post["theme"]), ("rendered", "mixed"))
        self.assertIn("quota exceeded", self.db.get("last_tag_error"))

    def test_make_tags_photos_right_away(self):
        self._fill("library", 6)
        images.scan(self.cfg, self.db)
        with mock.patch("engine.producer.render", return_value=10.0):
            produce(self.cfg, self.db, FakeAI(), self.pb, self.today, rng=random.Random(2))
        self.assertGreaterEqual(sum(1 for r in self.db.assets("library") if r["title"]), 4)

    def test_out_of_season_theme_never_used_as_fallback(self):
        self._fill("library", 5)
        images.scan(self.cfg, self.db)
        self.db.x("UPDATE assets SET title='Ёлка', theme='newyear'")
        weights = pbm.theme_weights(self.pb, self.today)  # сентябрь
        rows, theme = images.pick_library(self.db, "cinema", 4, random.Random(1), weights)
        self.assertEqual(rows, [])

    def test_custom_core_prompt_overrides_default(self):
        self.assertIn("ЯДРО", images.core_prompt(self.cfg))
        (self.cfg.data_dir / "core_prompt.md").write_text("МОЙ ПРОМПТ", encoding="utf-8")
        self.assertEqual(images.core_prompt(self.cfg), "МОЙ ПРОМПТ")


if __name__ == "__main__":
    unittest.main()
