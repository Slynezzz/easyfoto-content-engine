import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import music
from engine.admin_bot import AdminBot, Services
from engine.config import Config
from engine.db import DB


class MusicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(data_dir=self.tmp, admin_chat_id=5)
        self.repo = mock.patch.object(music, "REPO_MUSIC", self.tmp / "none")
        self.repo.start()

    def tearDown(self):
        self.repo.stop()

    def _track(self, mood, name="t.mp3"):
        p = music.music_dir(self.cfg) / mood / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"ID3")
        return p

    def test_mood_from_caption(self):
        self.assertEqual(music.mood_from_caption("Музыка мистика для хэллоуина"), "mystic")
        self.assertEqual(music.mood_from_caption("музыка новогодняя"), "festive")
        self.assertEqual(music.mood_from_caption("музыка"), "upbeat")

    def test_pick_prefers_mood_then_fallback(self):
        rng = random.Random(1)
        self.assertIsNone(music.pick(self.cfg, "mystic", rng))
        epic = self._track("epic")
        self.assertEqual(music.pick(self.cfg, "mystic", rng), epic)   # мистики нет — берём эпичную
        mystic = self._track("mystic")
        self.assertEqual(music.pick(self.cfg, "mystic", rng), mystic)
        self.assertIn(music.pick(self.cfg, None, rng), (epic, mystic))
        self.assertIn("мистика: 1", music.summary(self.cfg))

    def test_upload_through_admin_bot(self):
        db = DB(self.cfg.db_path)
        api = mock.Mock()
        said = []
        api.send_message.side_effect = lambda c, t, m=None: said.append(t) or {"message_id": 1}
        api.download.side_effect = lambda fid, dest: (dest.parent.mkdir(parents=True, exist_ok=True),
                                                      dest.write_bytes(b"ID3"))
        bot = AdminBot(self.cfg, db, api, Services(make_one=lambda: 0, instagram_ready=lambda: False,
                                                   run_analysis=lambda: "", stats_text=lambda: ""))
        bot.handle({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 5, "type": "private"},
                                                "audio": {"file_id": "a", "file_name": "Song.MP3", "file_size": 3000},
                                                "caption": "музыка романтика"}})
        self.assertEqual(len(list((music.music_dir(self.cfg) / "romantic").glob("*.mp3"))), 1)
        self.assertIn("романтичная", said[-1])
        # mp3 файлом с подписью — тоже музыка, а не «это не фото»
        bot.handle({"update_id": 2, "message": {"message_id": 2, "chat": {"id": 5, "type": "private"},
                                                "document": {"file_id": "d", "file_name": "x.mp3", "file_size": 10},
                                                "caption": "Музыка бодрая"}})
        self.assertEqual(len(list((music.music_dir(self.cfg) / "upbeat").glob("*"))), 1)
        db.close()


if __name__ == "__main__":
    unittest.main()
