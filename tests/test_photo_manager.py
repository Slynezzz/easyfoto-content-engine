import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import images
from engine.admin_bot import AdminBot, Services
from engine.config import Config
from engine.db import DB
from tests.fixtures import make

ADMIN = 7


def cb(data):
    return {"update_id": 1, "callback_query": {"id": "c", "from": {"id": ADMIN}, "data": data,
                                                "message": {"message_id": 3}}}


class PhotoManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(data_dir=self.tmp, admin_chat_id=ADMIN)
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        for i in range(12):
            make(self.cfg.assets_dir / "library" / f"l{i:02d}.jpg", (600, 900), i)
        make(self.cfg.assets_dir / "before" / "b.jpg", (600, 900), 1)
        images.scan(self.cfg, self.db)
        self.api = mock.Mock()
        self.said = []
        self.api.send_message.side_effect = lambda chat, text, markup=None: self.said.append((text, markup)) or {"message_id": 1}
        self.bot = AdminBot(self.cfg, self.db, self.api, Services(
            make_one=lambda: 0, instagram_ready=lambda: False, run_analysis=lambda: "", stats_text=lambda: ""))

    def tearDown(self):
        self.db.close()

    def test_menu_lists_folders_with_counts(self):
        self.bot.handle({"update_id": 1, "message": {"message_id": 1, "chat": {"id": ADMIN, "type": "private"},
                                                     "text": "/photos"}})
        text, markup = self.said[-1]
        labels = [b[0]["text"] for b in markup["inline_keyboard"]]
        self.assertIn("Библиотека (12)", labels)
        self.assertIn("Селфи «до» (1)", labels)
        self.assertIn("в ролики не попадают никогда", text)

    def test_page_sends_album_and_numbered_buttons(self):
        self.bot.handle(cb("pf:library:0"))
        paths, captions = self.api.send_media_group.call_args.args[1], self.api.send_media_group.call_args.args[2]
        self.assertEqual(len(paths), 10)
        self.assertEqual(captions[0], "1.")
        self.assertFalse(any(Path(p).exists() for p in paths))  # временные миниатюры убраны
        text, markup = self.said[-1]
        self.assertIn("1–10 из 12", text)
        self.assertEqual(markup["inline_keyboard"][-1][0]["callback_data"], "pf:library:10")
        self.bot.handle(cb("pf:library:10"))
        self.assertIn("11–12 из 12", self.said[-1][0])

    def test_delete_goes_to_trash(self):
        row = self.db.assets("library")[0]
        self.bot.handle(cb(f"pa:{row['id']}"))
        buttons = [b["callback_data"] for r in self.said[-1][1]["inline_keyboard"] for b in r]
        self.assertIn(f"pd:{row['id']}", buttons)
        self.assertIn(f"pm:{row['id']}:ref", buttons)
        self.bot.handle(cb(f"pd:{row['id']}"))
        self.assertEqual(len(self.db.assets("library")), 11)
        self.assertEqual(len(list((self.tmp / "trash").glob("*"))), 1)
        self.bot.handle(cb(f"pd:{row['id']}"))
        self.assertIn("уже нет", self.said[-1][0])

    def test_move_before_to_refs(self):
        row = self.db.assets("before")[0]
        self.bot.handle(cb(f"pm:{row['id']}:ref"))
        self.assertEqual(len(self.db.assets("ref")), 1)
        self.assertEqual(len(self.db.assets("before")), 0)
        self.assertIn("/refs/", self.db.assets("ref")[0]["path"])
        self.assertTrue(Path(self.db.assets("ref")[0]["path"]).exists())

    def test_strangers_cannot_manage_photos(self):
        evil = cb("pd:1")
        evil["callback_query"]["from"]["id"] = 999
        self.bot.handle(evil)
        self.assertEqual(len(self.db.assets("library")), 12)


if __name__ == "__main__":
    unittest.main()
