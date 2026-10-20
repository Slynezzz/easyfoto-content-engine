import base64
import io
import json
import random
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from PIL import Image

from engine import images, occasions, playbook as pbm, scout
from engine.app import Engine
from engine.config import Config
from engine.db import DB
from engine.llm import AIError, ImageResult
from engine.producer import ProduceError, produce
from tests.fixtures import make

TZ = ZoneInfo("Europe/Moscow")


class CalendarTests(unittest.TestCase):
    def test_movable_dates(self):
        self.assertEqual(occasions.orthodox_easter(2025), date(2025, 4, 20))
        self.assertEqual(occasions.orthodox_easter(2026), date(2026, 4, 12))
        self.assertEqual(occasions.orthodox_easter(2027), date(2027, 5, 2))
        cal = {e["id"]: e for e in occasions._calendar()}
        self.assertEqual(occasions.resolve(cal["maslenitsa"], 2026), date(2026, 2, 22))
        self.assertEqual(occasions.resolve(cal["medics_day"], 2026), date(2026, 6, 21))
        self.assertEqual(occasions.resolve(cal["mothers_day"], 2026), date(2026, 11, 29))
        self.assertEqual(occasions.resolve(cal["programmer_day"], 2026), date(2026, 9, 13))
        self.assertEqual(occasions.resolve(cal["programmer_day"], 2028), date(2028, 9, 12))  # високосный

    def test_active_windows(self):
        self.assertEqual(occasions.active(date(2026, 12, 20))[0].id, "new_year")
        self.assertEqual(occasions.active(date(2027, 1, 5))[0].id, "new_year")   # после праздника ещё неделя
        self.assertIn("halloween", [o.id for o in occasions.active(date(2026, 10, 25))])
        self.assertEqual(occasions.active(date(2026, 8, 20)), [])
        march = [o.id for o in occasions.active(date(2026, 3, 5))]
        self.assertEqual(march[0], "march_8")

    def test_every_calendar_entry_is_valid(self):
        for e in occasions._calendar():
            with self.subTest(e["id"]):
                occasions.resolve(e, 2027)
                self.assertGreaterEqual(len(e["scenes"]), 2)

    def test_events_from_scout_and_find(self):
        events = [{"id": "ev1", "title": "Первый снег", "date": "2026-11-12", "scenes": ["a", "b"], "lead": 3}]
        self.assertIn("ev1", [o.id for o in occasions.active(date(2026, 11, 10), events)])
        self.assertEqual(occasions.find("хэллоуин", date(2026, 10, 20)).id, "halloween")
        self.assertEqual(occasions.find("снег", date(2026, 10, 20), events).source, "trend")
        self.assertIsNone(occasions.find("несуществующий", date(2026, 10, 20)))


def png():
    buf = io.BytesIO()
    Image.new("RGB", (600, 1000), (90, 60, 120)).save(buf, "JPEG")
    return buf.getvalue()


class FakeAI:
    def __init__(self, fail_images=False):
        self.prompts, self.fail_images = [], fail_images

    def json(self, system, prompt, images=None, temperature=0.9):
        self.prompts.append(prompt)
        if images:
            return {"title": "Образ", "theme": "cinema", "description": "x"}
        res = {"hook": "Хук к *празднику*", "captions": ["a"] * 6, "cta_title": "Хочешь?", "cta_sub": "Бот в профиле",
               "post_caption": "Текст поста про повод и бота по ссылке в профиле. " * 3, "hashtags": ["#x"]}
        if '"scenes"' in prompt:
            res["scenes"] = [{"title": f"Сцена {i}", "where": "w", "wearing": "c", "doing": "d", "around": "a",
                              "light": "l"} for i in range(6)]
        return res

    def image(self, prompt, refs):
        if self.fail_images:
            raise AIError("генерация недоступна")
        return ImageResult(png(), "image/jpeg", "fake")


class OccasionProduceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), generate_photos=4)
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.pb = pbm.load(self.cfg.playbook_path)
        make(self.cfg.assets_dir / "before" / "me.jpg", (900, 1200), 2)
        images.scan(self.cfg, self.db)
        self.today = date(2026, 10, 25)
        self.hw = occasions.find("хэллоуин", self.today)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_occasion_reel_generates_new_photos_in_theme(self):
        ai = FakeAI()
        with mock.patch("engine.producer.render", return_value=8.0):
            pid = produce(self.cfg, self.db, ai, self.pb, self.today, random.Random(1), occasion=self.hw)
        post = self.db.post(pid)
        self.assertEqual(post["theme"], "occ:halloween")
        gen = self.db.assets("generated")
        self.assertEqual(len(gen), 4)
        self.assertTrue(all(g["theme"] == "occ:halloween" for g in gen))
        self.assertIn("ПОВОД РОЛИКА: Хэллоуин", ai.prompts[-1])
        # праздничные кадры не всплывают в обычной библиотеке вне повода
        rows, _ = images.pick_library(self.db, "cinema", 3, random.Random(1))
        self.assertEqual(rows, [])
        # рабочий плейбук не испорчен временной темой
        self.assertNotIn("occ:halloween", pbm.load(self.cfg.playbook_path)["themes"])

    def test_occasion_needs_face_photos(self):
        self.db.x("DELETE FROM assets")
        with self.assertRaises(ProduceError) as ctx:
            produce(self.cfg, self.db, FakeAI(), self.pb, self.today, occasion=self.hw)
        self.assertIn("«реф»", str(ctx.exception))

    def test_generation_failure_is_explained(self):
        with mock.patch("engine.producer.render", return_value=8.0):
            with self.assertRaises(ProduceError) as ctx:
                produce(self.cfg, self.db, FakeAI(fail_images=True), self.pb, self.today, occasion=self.hw)
        self.assertIn("генерация недоступна", str(ctx.exception))

    def test_mixed_mode_generates_then_reuses_generated(self):
        self.cfg.image_source, self.cfg.generate_share = "mixed", 1.0
        with mock.patch("engine.producer.render", return_value=8.0):
            produce(self.cfg, self.db, FakeAI(), self.pb, date(2026, 9, 10), random.Random(3))
        self.assertGreaterEqual(len(self.db.assets("generated")), 3)
        self.cfg.generate_share = 0.0  # теперь из библиотеки — в ней уже есть новые кадры
        with mock.patch("engine.producer.render", return_value=8.0):
            pid = produce(self.cfg, self.db, FakeAI(), self.pb, date(2026, 9, 10), random.Random(4))
        self.assertEqual(self.db.post(pid)["status"], "rendered")


class OccasionScheduleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), publish_hours="12,19", reels_per_day=2, plan_hour=8)
        self.db = DB(self.cfg.db_path)
        self.made = []
        clock = lambda: datetime(2026, 10, 25, 9, 0, tzinfo=TZ)  # noqa: E731
        self.engine = Engine(self.cfg, self.db, ai=None, ig=mock.Mock(ready=lambda: False), now=clock)

        def fake_make(scheduled_for=None, occasion=None):
            self.made.append(occasion.id if occasion else None)
            return self.db.create_post(status="rendered", format="w", hook_id="h",
                                       theme=occasion.theme_id if occasion else "t", script_json={},
                                       scheduled_for=scheduled_for)
        self.engine.make_one = fake_make

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_one_slot_per_day_goes_to_occasion(self):
        self.engine.job_plan()
        self.assertEqual(self.made, ["halloween", None])

    def test_occasion_failure_falls_back_to_regular_reel(self):
        calls = []

        def flaky(scheduled_for=None, occasion=None):
            calls.append(occasion.id if occasion else None)
            if occasion:
                raise ProduceError("генерация недоступна")
            return self.db.create_post(status="rendered", format="w", hook_id="h", theme="t", script_json={},
                                       scheduled_for=scheduled_for)
        self.engine.make_one = flaky
        self.engine.bot = mock.Mock()
        self.engine.job_plan()
        self.assertEqual(calls, ["halloween", None, None])
        self.assertTrue(any("не собрался" in str(c) for c in self.engine.bot.say.call_args_list))

    def test_make_for_and_occasions_text(self):
        self.engine.make_for("повод")
        self.engine.make_for("8 марта")
        self.assertEqual(self.made, ["halloween", "march_8"])
        with self.assertRaises(ValueError):
            self.engine.make_for("неизвестно")
        text = self.engine.occasions_text()
        self.assertIn("● 31.10 — Хэллоуин", text)


class ScoutEventsTests(unittest.TestCase):
    def test_merge_events_filters_and_expires(self):
        pb = {"events": [{"id": "old", "title": "Старое", "date": "2026-09-01", "scenes": ["a", "b"]}]}
        added = scout.merge_events(pb, [
            {"title": "Первый снег", "date": "2026-11-05", "scenes": ["a", "b", "c"], "why": "сезон"},
            {"title": "Без даты", "scenes": ["a", "b"]},
            {"title": "Слишком далеко", "date": "2027-06-01", "scenes": ["a", "b"]},
            {"title": "Мало сцен", "date": "2026-11-01", "scenes": ["a"]},
            {"title": "первый снег", "date": "2026-11-06", "scenes": ["a", "b"]},
        ], date(2026, 10, 25))
        self.assertEqual(added, ["05.11 — Первый снег"])
        self.assertEqual([e["title"] for e in pb["events"]], ["Первый снег"])
        self.assertIn("ev1", [o.id for o in occasions.active(date(2026, 11, 3), [dict(pb["events"][0], id="ev1")])])


if __name__ == "__main__":
    unittest.main()
