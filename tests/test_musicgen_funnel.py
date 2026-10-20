import random
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from engine import funnel, music, musicgen, scout
from engine.config import Config
from engine.db import DB
from engine.llm import media_models

MP3 = b"ID3" + b"\x00" * 20000


class Resp:
    def __init__(self, status, content=b"", data=None):
        self.status_code, self.content, self._data, self.text = status, content, data, str(data)

    def json(self):
        if self._data is None:
            raise ValueError
        return self._data


class MusicGenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(data_dir=self.tmp, music_api_key="xi", max_music_per_day=2)
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)

    def tearDown(self):
        self.db.close()

    def test_generates_track_into_library_and_counts_budget(self):
        with mock.patch("engine.musicgen.requests.post", return_value=Resp(200, MP3)) as post:
            path = musicgen.track_for(self.cfg, self.db, {}, "mystic", 14.5, random.Random(1))
        body = post.call_args.kwargs["json"]
        self.assertTrue(body["force_instrumental"])
        self.assertEqual(body["music_length_ms"], 16500)
        self.assertIn("trap", body["prompt"])
        self.assertEqual(post.call_args.kwargs["headers"]["xi-api-key"], "xi")
        self.assertNotIn("Authorization", post.call_args.kwargs["headers"])  # ElevenLabs: только один заголовок
        self.assertTrue(path.exists())
        self.assertEqual(path.parent.name, "mystic")
        self.assertIn(path, music.tracks(self.cfg)["mystic"])
        self.assertEqual(musicgen.used_today(self.db), 1)

    def test_trend_style_for_regular_reel(self):
        pb = {"music_styles": [{"style": "phonk with cowbell, 150 bpm"}]}
        self.assertEqual(musicgen.choose_style(pb, None, random.Random()), ("phonk with cowbell, 150 bpm", "trend"))
        self.assertEqual(musicgen.choose_style({}, None, random.Random())[1], "upbeat")

    def test_limit_and_errors_fall_back_to_library(self):
        own = music.music_dir(self.cfg) / "upbeat" / "mine.mp3"
        own.parent.mkdir(parents=True)
        own.write_bytes(MP3)
        with mock.patch("engine.musicgen.requests.post",
                        return_value=Resp(400, data={"detail": {"message": "bad_prompt"}})):
            got = musicgen.track_for(self.cfg, self.db, {}, None, 10, random.Random(1))
        self.assertEqual(got, own)
        self.assertIn("bad_prompt", self.db.get("last_music_error"))
        self.assertEqual(musicgen.used_today(self.db), 0)  # неудача лимит не тратит
        self.db.set(musicgen._day_key(), "2")
        with mock.patch("engine.musicgen.requests.post") as post:
            self.assertEqual(musicgen.track_for(self.cfg, self.db, {}, None, 10, random.Random(1)), own)
        post.assert_not_called()

    def test_non_audio_is_rejected(self):
        with mock.patch("engine.musicgen.requests.post", return_value=Resp(200, b"<html>oops</html>")):
            with self.assertRaises(musicgen.MusicError):
                musicgen.generate(self.cfg, self.db, "x", "upbeat", 10)

    def test_off_without_key(self):
        cfg = Config(data_dir=self.tmp, music_api_key="")
        with mock.patch("engine.musicgen.requests.post") as post:
            self.assertIsNone(musicgen.track_for(cfg, self.db, {}, None, 10, random.Random()))
        post.assert_not_called()
        self.assertIn("MUSIC_API_KEY", musicgen.status(cfg, self.db))


class ScoutMusicTests(unittest.TestCase):
    def test_music_styles_merged_without_song_names(self):
        pb = {"hooks": {}, "themes": {}, "formats": {"worlds": {}}, "events": []}
        res = {"music": ["sped-up pop with airy synths, 130 bpm", "в стиле «Хит лета»", ""]}
        added = scout.merge(pb, res, date(2026, 10, 20), datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.assertEqual([m["style"] for m in pb["music_styles"]], ["sped-up pop with airy synths, 130 bpm"])
        self.assertEqual(added["music"], ["sped-up pop with airy synths, 130 bpm"])


class FakeCursor:
    def __init__(self):
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.calls += 1
        self.sql = sql

    def fetchone(self):
        return (40, 22, 3)

    def fetchall(self):
        return [("ig", "bio", "", 25, 15, 2, 980.0), ("vk", "clip", "p12", 5, 1, 0, 0)]


class FunnelTests(unittest.TestCase):
    def test_start_param_fits_telegram_rules(self):
        self.assertEqual(funnel.start_param("ig"), "utm_ig_bio")
        p = funnel.start_param("vk", "clip", "p 12/хэллоуин" + "x" * 80)
        self.assertRegex(p, r"^[A-Za-z0-9_-]{1,64}$")

    def test_report_reads_bot_db_read_only(self):
        conn = mock.Mock()
        conn.cursor.return_value = FakeCursor()
        fake = types.SimpleNamespace(connect=mock.Mock(return_value=conn), Error=Exception)
        cfg = Config(bot_database_url="postgres://x", bot_username="Easyfotobot")
        with mock.patch.dict(sys.modules, {"psycopg2": fake}):
            text = funnel.report(cfg, 30)
        self.assertIn("default_transaction_read_only=on", fake.connect.call_args.kwargs["options"])
        conn.set_session.assert_called_with(readonly=True, autocommit=True)
        self.assertIn("40 → сделали фото 22 (55%) → купили 3 (8%)", text)
        self.assertIn("Instagram bio: 25 → 15 → 2, 980 ₽", text)
        self.assertIn("VK clip p12: 5 → 1 → 0", text)
        conn.close.assert_called_once()

    def test_report_without_db(self):
        self.assertIn("BOT_DATABASE_URL", funnel.report(Config(bot_database_url=""), 7))
        self.assertIn("https://t.me/Easyfotobot?start=utm_ig_bio", funnel.links_text(Config(bot_username="Easyfotobot")))


class MediaModelsTests(unittest.TestCase):
    def test_detects_video_music_voice(self):
        got = media_models(["gpt-4o", "kling-v2-1", "veo-3-fast", "suno-v4.5", "lyria-2", "gpt-4o-mini-tts", "gemini-3.8-flash"])
        self.assertEqual(got["видео из фото (оживление)"], ["kling-v2-1", "veo-3-fast"])
        self.assertEqual(got["музыка"], ["suno-v4.5", "lyria-2"])
        self.assertEqual(got["озвучка голосом"], ["gpt-4o-mini-tts"])


if __name__ == "__main__":
    unittest.main()
