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
            self.assertEqual(set(n), keys, nid)
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
        self.assertEqual(fb.script["cta_sub"], "Пишите — ссылка в профиле")
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
