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
            publish_now=lambda pid: (self.published.append(pid),
                                     self.db.update_post(pid, status="published"), "🚀 готово")[2],
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

    def test_publish_now(self):
        pid = self._post()
        self.bot.handle(cb(f"now:{pid}"))
        self.assertEqual(self.published, [pid])
        self.assertIn("🚀 Опубликовано", [k["label"] for m, k in self.api.sent if m == "markup"])

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


if __name__ == "__main__":
    unittest.main()
