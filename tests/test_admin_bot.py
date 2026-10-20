import json
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from engine.admin_bot import AdminBot, Services
from engine.config import Config
from engine.db import DB
from tests.fixtures import make

ADMIN = 111


class FakeAPI:
    def __init__(self, fixture: Path):
        self.sent = []          # (method, kwargs)
        self.updates = []
        self.fixture = fixture
        self._mid = 100

    def _msg(self):
        self._mid += 1
        return {"message_id": self._mid}

    def get_updates(self, offset, timeout=25):
        ups, self.updates = self.updates, []
        return ups

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(("message", {"chat_id": chat_id, "text": text, "markup": reply_markup}))
        return self._msg()

    def send_video(self, chat_id, path, caption="", reply_markup=None, cover=None):
        self.sent.append(("video", {"chat_id": chat_id, "caption": caption, "markup": reply_markup}))
        return self._msg()

    def edit_markup(self, chat_id, message_id, reply_markup):
        self.sent.append(("markup", {"label": reply_markup["inline_keyboard"][0][0]["text"]}))

    def delete_message(self, chat_id, message_id):
        self.sent.append(("delete", {"message_id": message_id}))

    def answer_callback(self, cid, text=""):
        self.sent.append(("answer", {"text": text}))

    def download(self, file_id, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.fixture.read_bytes())
        return dest

    def texts(self):
        return [k["text"] for m, k in self.sent if m == "message"]


def msg(text=None, chat=ADMIN, **extra):
    m = {"message_id": 1, "chat": {"id": chat, "type": "private"}, "from": {"id": chat}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"update_id": 1, "message": m}


def cb(data, user=ADMIN):
    return {"update_id": 2, "callback_query": {"id": "c", "from": {"id": user}, "data": data,
                                                "message": {"message_id": 55}}}


class AdminBotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.cfg = Config(data_dir=tmp / "data", admin_chat_id=ADMIN)
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.api = FakeAPI(make(tmp / "f.jpg", (600, 900)))
        self.ig_ready = True
        self.published = []
        self.made = []
        self.bot = AdminBot(self.cfg, self.db, self.api, Services(
            make_one=self._make_one,
            instagram_ready=lambda: self.ig_ready,
            run_analysis=lambda: "analysis",
            stats_text=lambda: "stats",
        ))

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _make_one(self):
        pid = self._post(status="rendered")
        self.made.append(pid)
        return pid

    def _post(self, status="pending", scheduled=None):
        plan = {"script": {"hook": "Хук"}, "post_caption": "Текст поста", "choice": {}}
        return self.db.create_post(status=status, format="worlds", hook_id="eras", theme="eras",
                                   script_json=plan, video_path="v.mp4", cover_path="c.jpg",
                                   caption="Текст поста #нейрофото", scheduled_for=scheduled)

    def test_unknown_admin_gets_chat_id_hint(self):
        self.cfg.admin_chat_id = 0
        self.bot.handle(msg("/start", chat=777))
        self.assertIn("777", self.api.texts()[0])

    def test_strangers_are_ignored(self):
        self.bot.handle(msg("/make", chat=999))
        self.bot.handle(cb("ok:1", user=999))
        self.assertEqual(self.made, [])
        self.assertEqual([m for m, _ in self.api.sent], ["answer"])

    def test_send_for_approval_sets_pending_with_keyboard(self):
        pid = self._post(status="rendered", scheduled="2026-10-20 12:00")
        self.assertTrue(self.bot.send_for_approval(pid))
        kind, kw = self.api.sent[-1]
        self.assertEqual(kind, "video")
        self.assertIn("2026-10-20 12:00", kw["caption"])
        datas = [b["callback_data"] for row in kw["markup"]["inline_keyboard"] for b in row]
        self.assertIn(f"ok:{pid}", datas)
        self.assertEqual(self.db.post(pid)["status"], "pending")
        self.assertFalse(self.bot.send_for_approval(pid))  # повторно не шлём

    def test_failed_upload_returns_post_to_queue(self):
        pid = self._post(status="rendered")
        self.api.send_video = mock.Mock(side_effect=RuntimeError("network"))
        self.assertFalse(self.bot.send_for_approval(pid))
        self.assertEqual(self.db.post(pid)["status"], "rendered")

    def test_concurrent_sends_deliver_once(self):
        pid = self._post(status="rendered")
        self.db.claim_post(pid, "rendered", "sending")  # другой поток уже отправляет
        self.assertFalse(self.bot.send_for_approval(pid))
        self.assertEqual([m for m, _ in self.api.sent], [])

    def test_approve_goes_to_queue(self):
        pid = self._post()
        self.bot.handle(cb(f"ok:{pid}"))
        self.assertEqual(self.db.post(pid)["status"], "approved")
        self.bot.handle(cb(f"ok:{pid}"))  # двойное нажатие
        self.assertIn("Уже обработан", [k["text"] for m, k in self.api.sent if m == "answer"])

    def test_publish_now_hands_over_to_scheduler(self):
        pid = self._post(scheduled="2026-12-31 19:00")
        self.bot.handle(cb(f"now:{pid}"))
        post = self.db.post(pid)
        self.assertEqual(post["status"], "approved")
        self.assertNotEqual(post["scheduled_for"], "2026-12-31 19:00")  # перенесён на «сейчас»
        self.assertIn("⏳ Публикуется…", [k["label"] for m, k in self.api.sent if m == "markup"])

    def test_publish_now_respects_pause(self):
        self.db.set("paused", "1")
        pid = self._post()
        self.bot.handle(cb(f"now:{pid}"))
        self.assertIn("✅ Выйдет после /resume", [k["label"] for m, k in self.api.sent if m == "markup"])

    def test_retry_failed_post(self):
        pid = self._post(status="failed")
        self.bot.handle(cb(f"retry:{pid}"))
        self.assertEqual(self.db.post(pid)["status"], "approved")
        self.assertIsNotNone(self.db.post(pid)["scheduled_for"])

    def test_manual_mode_when_instagram_not_connected(self):
        self.ig_ready = False
        pid = self._post()
        self.bot.handle(cb(f"now:{pid}"))
        self.assertEqual(self.published, [])
        self.assertEqual(self.db.post(pid)["status"], "manual")
        self.assertIn("Текст поста #нейрофото", self.api.texts())

    def test_redo_rejects_and_makes_new_with_same_slot(self):
        pid = self._post(scheduled="2026-10-20 19:00")
        self.bot.handle(cb(f"redo:{pid}"))
        self.assertEqual(self.db.post(pid)["status"], "rejected")
        new = self.made[-1]
        self.assertEqual(self.db.post(new)["scheduled_for"], "2026-10-20 19:00")
        self.assertEqual(self.db.post(new)["status"], "pending")

    def test_make_failure_is_reported_not_raised(self):
        def boom():
            raise RuntimeError("не хватает фото")
        self.bot.s.make_one = boom
        self.bot.handle(msg("/make"))
        self.assertTrue(any("не хватает фото" in t for t in self.api.texts()))

    def test_photo_upload_routes_by_caption_and_album(self):
        photo = {"photo": [{"file_id": "small"}, {"file_id": "big"}]}
        self.bot.handle(msg(caption="до", media_group_id="g1", **photo))
        self.bot.handle(msg(media_group_id="g1", **photo))   # без подписи, тот же альбом
        self.bot.handle(msg(caption="реф", **photo))
        self.bot.handle(msg(**photo))
        self.assertEqual(len(self.db.assets("before")), 2)
        self.assertEqual(len(self.db.assets("ref")), 1)
        self.assertEqual(len(self.db.assets("library")), 1)

    def test_move_last_uploaded_to_before(self):
        photo = {"photo": [{"file_id": "big"}]}
        for i in range(7):
            m = msg(**photo)
            m["message"]["message_id"] = 500 + i
            self.bot.handle(m)
        self.bot.handle(msg("/move 5 до"))
        self.assertEqual(len(self.db.assets("before")), 5)
        self.assertEqual(len(self.db.assets("library")), 2)
        for r in self.db.assets("before"):
            self.assertTrue(Path(r["path"]).exists())
            self.assertIn("/before/", r["path"])
        self.assertIn("Перенёс 5", self.api.texts()[-1])

    def test_move_by_reply(self):
        m = msg(photo=[{"file_id": "big"}])
        m["message"]["message_id"] = 900
        self.bot.handle(m)
        self.bot.handle(msg("реф", reply_to_message={"message_id": 900}))
        self.assertEqual(len(self.db.assets("ref")), 1)
        self.bot.handle(msg("что-то", reply_to_message={"message_id": 900}))
        self.assertIn("одним словом", self.api.texts()[-1])

    def test_move_usage_hint(self):
        self.bot.handle(msg("/move до"))
        self.assertIn("/move 5 до", self.api.texts()[-1])

    def test_status_shows_tagging_error(self):
        self.db.set("last_tag_error", "не получилось разметить фото: API key not valid")
        self.bot.handle(msg("/status"))
        self.assertIn("API key not valid", self.api.texts()[-1])

    def test_rejects_non_image_documents(self):
        self.bot.handle(msg(document={"file_id": "x", "file_name": "doc.pdf", "file_size": 10}))
        self.assertTrue(any("Это не фото" in t for t in self.api.texts()))

    def test_poll_advances_offset_even_if_handler_fails(self):
        self.api.updates = [{"update_id": 41, "message": None}, {"update_id": 42, "callback_query": "broken"}]
        self.assertEqual(self.bot.poll_once(timeout=0), 2)
        self.assertEqual(self.db.get("tg_offset"), "43")

    def test_pause_resume_and_status(self):
        self.bot.handle(msg("/pause"))
        self.assertTrue(self.bot.paused)
        self.bot.handle(msg("/status"))
        self.assertIn("на паузе", self.api.texts()[-1])
        self.bot.handle(msg("/resume"))
        self.assertFalse(self.bot.paused)

    def test_approved_note_lands_in_brief_once(self):
        import json as js
        from engine import brief
        self.db.set("brief_proposals", js.dumps(["Сегмент «аватарка» конвертирует лучше всего", "Второе"]))
        self.assertEqual(self.bot.send_brief_proposals(), 2)
        self.bot.handle(cb("bok:0"))
        self.bot.handle(cb("bok:0"))  # повторное нажатие
        self.bot.handle(cb("bno:1"))
        text = brief.load(self.cfg)
        self.assertEqual(text.count("Сегмент «аватарка» конвертирует лучше всего"), 1)
        self.assertNotIn("Второе", text)
        self.assertIn("3 бесплатные генерации", text)  # дописали к стандартному брифу
        self.assertEqual(self.bot.send_brief_proposals(), 0)

    def test_stranger_cannot_approve(self):
        import json as js
        from engine import brief
        self.db.set("brief_proposals", js.dumps(["X"]))
        self.bot.handle(cb("bok:0", user=999))
        self.assertFalse(brief.is_custom(self.cfg))


    def test_long_caption_is_sent_in_full(self):
        long = "Очень длинный текст поста. " * 60 + "по ссылке в профиле"
        pid = self.db.create_post(status="rendered", format="worlds", hook_id="h", theme="t",
                                  script_json={"script": {"hook": "Хук"}, "post_caption": long, "choice": {}},
                                  video_path="v.mp4", cover_path="c.jpg", caption=long)
        self.assertTrue(self.bot.send_for_approval(pid))
        video = [k for m, k in self.api.sent if m == "video"][-1]
        self.assertLessEqual(len(video["caption"]), 1024)
        self.assertTrue(self.api.texts()[-1].endswith("по ссылке в профиле"))


    def test_vk_token_message_is_deleted_and_saved(self):
        got = []
        self.bot.s.vk_connect = lambda text: got.append(text) or "✅ VK подключён"
        self.bot.handle(msg("https://oauth.vk.com/blank.html#access_token=vk1.SECRET&expires_in=0&user_id=1"))
        self.assertIn(("delete", {"message_id": 1}), self.api.sent)
        self.assertEqual(len(got), 1)
        self.assertEqual(self.api.texts()[-1], "✅ VK подключён")

    def test_vk_help_without_app(self):
        self.cfg.vk_app_id = ""
        self.bot.handle(msg("/vk"))
        self.assertIn("VK_APP_ID", self.api.texts()[-1])
        self.cfg.vk_app_id = "55"
        self.bot.handle(msg("/vk"))
        self.assertIn("client_id=55", self.api.texts()[-1])

    def test_approval_queues_other_platforms(self):
        pid = self._post()
        self.bot.s.on_approved = lambda p, now: f"📘 VK: {'сейчас' if now else 'по расписанию'}"
        self.bot.handle(cb(f"ok:{pid}"))
        self.assertIn("📘 VK: по расписанию", self.api.texts())


if __name__ == "__main__":
    unittest.main()
