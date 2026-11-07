import json
import random
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from engine import brief, niche, occasions, planner, playbook as pbm, scout
from engine.app import Engine
from engine.config import Config
from engine.db import DB
from engine.telegram import TelegramError
from tests.test_engine import TZ, Clock, FakeBot, FakeIG


class NicheTests(unittest.TestCase):
    def tearDown(self):
        niche.activate(Config(niche="neurophoto"))   # не влиять на другие тесты

    def test_all_niches_load_and_have_fields(self):
        keys = set(niche.load("neurophoto"))
        for nid in niche.available():
            n = niche.load(nid)
            self.assertTrue(keys <= set(n), nid)
            # необязательные поля — только у ниш с ботом для заявок
            self.assertTrue(all(k.startswith("lead_") or k in ("posts", "caption_limit") for k in set(n) - keys), nid)
            self.assertTrue(niche.playbook_path(n).exists())
            self.assertTrue(niche.brief_path(n).exists())
        with self.assertRaises(niche.NicheError):
            niche.load("нет-такой")

    def test_expert_playbook_and_brief_activate(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(data_dir=Path(tmp), niche="expert")
            niche.activate(cfg)
            pb = pbm.load(cfg.playbook_path)
            self.assertNotIn("before_after", pb["formats"])   # «до/после» — только для нейрофото
            self.assertIn("smb_owner", pb["audiences"])
            self.assertIn("AI-архитектор у моря", brief.load(cfg))
            for h in pb["hooks"].values():
                self.assertTrue(set(h["formats"]) <= set(pb["formats"]))

    def test_expert_prompts_have_no_neurophoto_bot_talk(self):
        n = niche.load("expert")
        pb = json.loads(niche.playbook_path(n).read_text(encoding="utf-8"))
        choice = pbm.choose(pb, date(2026, 11, 6), 0.0, random.Random(1))
        prompt = planner.build_prompt(pb, choice, 5, None, True, "male", n)
        self.assertNotIn("бот по ссылке", prompt)
        self.assertIn("как в брифе", prompt)
        fb = planner.fallback(pb, choice, 5, None, "Easyfotobot", n)
        self.assertNotIn("селфи", fb.post_caption)
        self.assertEqual(fb.script["cta_sub"], "Бесплатно — ссылка в профиле")
        qs = scout.search_queries(date(2026, 11, 6), n)
        self.assertIn("ИИ для малого бизнеса новости ноябрь 2026", qs)
        self.assertFalse(any("нейрофото" in q for q in qs))
        # нейрофото по умолчанию не изменилось
        self.assertIn("нейрофотосессия тренд 2026", scout.search_queries(date(2026, 11, 6)))

    def test_occasions_filtered_by_niche(self):
        n = niche.load("expert")
        act = occasions.active(date(2026, 10, 25))
        self.assertIn("halloween", [o.id for o in act])
        self.assertFalse(niche.allows_occasion(n, "halloween"))
        self.assertTrue(niche.allows_occasion(n, "new_year"))
        self.assertTrue(niche.allows_occasion(n, "ev_1101_abcd"))
        self.assertTrue(niche.allows_occasion(niche.load("neurophoto"), "halloween"))


class PauseAndIllustrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), niche="expert", publish_hours="12,19", reels_per_day=2)
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_paused_engine_does_not_generate_but_scouts(self):
        clock = Clock(datetime(2026, 11, 6, 10, 0, tzinfo=TZ))
        engine = Engine(self.cfg, self.db, ai=None, ig=FakeIG(self.db), bot=FakeBot(), now=clock)
        self.db.set("gen_paused", "1")
        self.assertEqual(engine.job_plan(), 0)
        with self.assertRaises(RuntimeError):
            engine.make_one()

    def test_illustration_without_face_photos(self):
        import base64, io
        from PIL import Image
        from engine import images
        buf = io.BytesIO()
        Image.new("RGB", (64, 112), (10, 20, 30)).save(buf, "PNG")
        ai = mock.Mock()
        ai.image.return_value = mock.Mock(data=buf.getvalue(), provider="OpenAI/generations/gpt-image")
        self.assertTrue(images.illustration_mode(self.cfg))
        pid = images.generate(self.cfg, self.db, ai, {"title": "ИИ отвечает клиентам", "where": "кафе",
                                                     "doing": "смартфон с чат-ботом"}, "ai_tech")
        self.assertTrue(pid)
        prompt, refs = ai.image.call_args[0]
        self.assertEqual(refs, [])
        self.assertIn("Никакого текста", prompt)
        self.assertIn("смартфон с чат-ботом", prompt)

    def test_generations_endpoint_when_no_refs(self):
        from engine.llm import AI
        import base64, io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 112)).save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        calls = []

        def post(url, **kw):
            calls.append((url, kw.get("json")))
            r = mock.Mock(status_code=200, text="")
            r.json.return_value = {"data": [{"b64_json": b64}]}
            return r
        with mock.patch("engine.llm.requests.post", side_effect=post):
            res = AI._edits_image("https://img.example", "k", "gpt-image-1.5", "p", [])
        self.assertEqual(calls[0][0], "https://img.example/v1/images/generations")
        self.assertEqual(calls[0][1]["size"], "1024x1536")
        self.assertEqual(res.provider if hasattr(res, "provider") else res.mode, "generations")


class RequestTests(unittest.TestCase):
    def test_planner_gets_topic_and_platform_rules(self):
        n = niche.load("expert")
        pb = json.loads(niche.playbook_path(n).read_text(encoding="utf-8"))
        choice = pbm.choose(pb, date(2026, 11, 6), 0.0, random.Random(1))
        req = {"title": "Форум про ИИ для бизнеса 27.11", "angle": "что взять малому бизнесу",
               "url": "https://ex.ru/f", "platform": "ig"}
        prompt = planner.build_prompt(pb, choice, 5, None, True, "male", n, req)
        self.assertIn("ЗАКАЗ ВЛАДЕЛЬЦА", prompt)
        self.assertIn("Форум про ИИ для бизнеса 27.11", prompt)
        self.assertIn("Хэштегов не больше 5", prompt)
        plan = planner.Plan(choice={}, script={}, hashtags=["#a", "#b", "#c", "#d", "#e", "#f", "#g"])
        self.assertEqual(len(planner.fit_platform(plan, req).hashtags), 5)
        self.assertEqual(planner.fit_platform(plan, {"platform": "tg"}).hashtags, [])

    def test_scout_topics_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "e.db")
            n = scout.save_topics(db, {"topics": [
                {"kind": "news", "title": "Новость А", "angle": "как подать", "source": "https://a.ru"},
                {"kind": "problem | trend", "title": "Заявки теряются ночью", "source": "не ссылка"},
                {"title": ""}],
                "events": [{"date": "2026-11-27", "title": "Форум"}]})
            rows = db.topics(10)
            db.close()
        self.assertEqual(n, 3)
        kinds = {r["title"]: (r["kind"], r["url"]) for r in rows}
        self.assertEqual(kinds["Новость А"], ("news", "https://a.ru"))
        self.assertEqual(kinds["Заявки теряются ночью"], ("problem", ""))
        self.assertEqual(kinds["2026-11-27 — Форум"][0], "event")

    def test_auto_plan_off_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(data_dir=Path(tmp))
            cfg.ensure_dirs()
            db = DB(cfg.db_path)
            engine = Engine(cfg, db, ai=None, ig=FakeIG(db), bot=FakeBot(),
                            now=Clock(datetime(2026, 11, 6, 10, 0, tzinfo=TZ)))
            self.assertFalse(engine.auto_plan())
            self.assertEqual(engine.job_plan(), 0)
            db.set("auto_plan", "1")
            self.assertTrue(engine.auto_plan())
            db.close()


class TGApi:
    def __init__(self, fail=None):
        self.fail, self.videos, self.texts = fail, [], []

    def send_video(self, chat_id, path, caption="", reply_markup=None, cover=None):
        if self.fail:
            raise TelegramError(self.fail)
        self.videos.append((chat_id, caption))
        return {"message_id": 77}

    def send_message(self, chat_id, text, reply_markup=None):
        self.texts.append((chat_id, text))
        return {"message_id": 78}


class TGChannelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), publish_hours="12,19", reels_per_day=2,
                          tg_channel_id="@ai_architect", cta_url="https://t.me/maximus_karpov",
                          vk_manual=False, yt_client_id="")
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.clock = Clock(datetime(2026, 11, 6, 9, 0, tzinfo=TZ))
        self.bot = FakeBot()
        self.engine = Engine(self.cfg, self.db, ai=None, ig=FakeIG(self.db), bot=self.bot, now=self.clock)
        video = Path(self._tmp.name) / "v.mp4"
        video.write_bytes(b"\x00" * 10)
        self.long = "Первый абзац поста. " * 40 + "\n\n" + "Второй абзац с вопросом? " * 20
        self.pid = self.db.create_post(status="approved", format="worlds", hook_id="h", theme="t", script_json={},
                                       video_path=str(video), caption=self.long, scheduled_for="2026-11-06 12:00")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_publishes_to_channel_with_split_text_and_cta(self):
        api = TGApi()
        self.engine.tg.api = api
        self.assertEqual(self.engine.on_approved(self.pid, now=True), "✈️ Telegram-канал: сейчас")
        self.assertEqual(self.engine.job_crosspost(), 1)
        chat, caption = api.videos[0]
        self.assertEqual(chat, "@ai_architect")
        self.assertLessEqual(len(caption), 1024)
        self.assertTrue(api.texts[0][1].endswith("👉 https://t.me/maximus_karpov"))
        row = self.db.crosspost(self.pid, "tg")
        self.assertEqual((row["status"], row["url"]), ("published", "https://t.me/ai_architect/77"))
        # повтор не дублирует видео
        self.engine.tg.publish(self.pid)
        self.assertEqual(len(api.videos), 1)

    def test_rights_error_explained_with_retry(self):
        self.engine.tg.api = TGApi(fail="Bad Request: need administrator rights in the channel chat")
        self.engine.on_approved(self.pid, now=True)
        self.engine.job_crosspost()
        text, markup = self.bot.said[-1]
        self.assertIn("администратором канала", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], f"tgretry:{self.pid}")


if __name__ == "__main__":
    unittest.main()
