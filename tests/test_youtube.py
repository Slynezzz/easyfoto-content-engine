import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

from engine import youtube
from engine.app import Engine
from engine.config import Config
from engine.db import DB
from tests.test_engine import TZ, Clock, FakeBot, FakeIG


class R:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._d, self.headers = status, data or {}, headers or {}

    def json(self):
        return self._d


class Google:
    def __init__(self, upload_status=200):
        self.token_calls, self.meta, self.put_size, self.upload_status = [], None, 0, upload_status

    def post(self, url, data=None, json=None, headers=None, timeout=None, params=None):
        if url == youtube.TOKEN:
            self.token_calls.append(dict(data))
            n = len(self.token_calls)
            body = {"access_token": f"ya29.T{n}", "expires_in": 3599,
                    "scope": youtube.SCOPES}
            if data["grant_type"] == "authorization_code":
                body["refresh_token"] = "1//R"
            return R(200, body)
        if url.startswith(youtube.UPLOAD):
            self.meta = json
            return R(200, headers={"Location": "https://upload.example/session1"})
        raise AssertionError(url)

    def put(self, url, data=None, headers=None, timeout=None):
        self.put_size = len(data.read())
        if self.upload_status != 200:
            return R(self.upload_status, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}})
        return R(200, {"id": "abc123"})

    def request(self, method, url, params=None, headers=None, timeout=None):
        if url.endswith("/channels"):
            return R(200, {"items": [{"snippet": {"title": "Нейро-фотограф"}}]})
        if url.endswith("/videos"):
            return R(200, {"items": [{"id": "abc123", "statistics": {"viewCount": "1500", "likeCount": "40",
                                                                    "commentCount": "3"}}]})
        raise AssertionError(url)


class YouTubeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), publish_hours="12,19", reels_per_day=2,
                          yt_client_id="cid.apps.googleusercontent.com", yt_client_secret="sec",
                          public_base_url="https://engine.up.railway.app", vk_manual=False,
                          vk_app_id="", vk_access_token="")
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.clock = Clock(datetime(2026, 10, 20, 9, 0, tzinfo=TZ))
        self.bot = FakeBot()
        self.engine = Engine(self.cfg, self.db, ai=None, ig=FakeIG(self.db), bot=self.bot, now=self.clock)
        video = Path(self._tmp.name) / "v.mp4"
        video.write_bytes(b"\x01" * 2048)
        self.pid = self.db.create_post(status="approved", format="worlds", hook_id="h", theme="t",
                                       script_json={"script": {"hook": "Хотел *фото* на Хэллоуин"}},
                                       video_path=str(video), caption="Текст поста #нейрофото",
                                       scheduled_for="2026-10-20 12:00")
        self.g = Google()
        self.patches = [mock.patch("engine.youtube.requests.post", side_effect=self.g.post),
                        mock.patch("engine.youtube.requests.put", side_effect=self.g.put),
                        mock.patch("engine.youtube.requests.request", side_effect=self.g.request)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()
        self._tmp.cleanup()

    def login(self):
        link = self.engine.yt.start_login()
        q = {k: v[0] for k, v in parse_qs(link.split("?", 1)[1]).items()}
        self.assertEqual(q["redirect_uri"], "https://engine.up.railway.app/yt/callback")
        self.assertEqual((q["access_type"], q["prompt"]), ("offline", "consent"))
        self.assertIn("youtube.upload", q["scope"])
        return self.engine.yt_callback({"code": "4/xyz", "state": q["state"]})

    def test_login_bad_state_and_success(self):
        page = self.engine.yt_callback({"code": "c", "state": "чужой"})
        self.assertIn("не подключился", page)
        self.assertIn("✅ YouTube подключён", self.login())
        self.assertIn("канал «Нейро-фотограф»", self.bot.said[-1][0])
        self.assertTrue(self.engine.yt.ready())
        self.assertEqual(self.g.token_calls[-1]["client_secret"], "sec")
        self.assertIn("Нейро-фотограф", self.engine.yt.status())

    def test_publish_at_slot_with_shorts_title_and_link(self):
        self.login()
        self.assertEqual(self.engine.on_approved(self.pid, now=False), "▶️ YouTube Shorts: 20.10 12:00")
        self.assertEqual(self.engine.job_crosspost(), 0)
        self.clock.dt = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
        self.assertEqual(self.engine.job_crosspost(), 1)
        sn = self.g.meta["snippet"]
        self.assertEqual(sn["title"], "Хотел фото на Хэллоуин #shorts")
        self.assertIn("https://t.me/Easyfotobot?start=utm_yt_short_p1", sn["description"])
        self.assertEqual(self.g.meta["status"]["privacyStatus"], "public")
        self.assertEqual(self.g.put_size, 2048)
        row = self.db.crosspost(self.pid, "yt")
        self.assertEqual((row["status"], row["url"]), ("published", "https://youtube.com/shorts/abc123"))
        self.assertEqual(self.engine.job_yt_metrics(), 1)
        m = self.db.one("SELECT * FROM platform_metrics WHERE post_id=?", (self.pid,))
        self.assertEqual((m["views"], m["likes"], m["comments"]), (1500, 40, 3))

    def test_quota_postpones_and_other_errors_offer_retry(self):
        self.login()
        self.g.upload_status = 403
        self.engine.on_approved(self.pid, now=True)
        self.engine.job_crosspost()
        row = self.db.crosspost(self.pid, "yt")
        self.assertEqual(row["status"], "queued")          # квота — сам повторит через сутки
        self.assertTrue(row["due_at"].startswith("2026-10-21"))
        self.assertIn("отложен", self.bot.said[-1][0])
        # прочая ошибка — кнопка повтора
        self.db.update_crosspost(self.pid, "yt", due_at="2026-01-01T00:00:00+00:00")
        self.g.upload_status = 500
        self.g.put = lambda *a, **k: R(500, {"error": {"message": "backend", "errors": [{"reason": "backendError"}]}})
        with mock.patch("engine.youtube.requests.put", side_effect=self.g.put):
            self.engine.job_crosspost()
        self.assertEqual(self.db.crosspost(self.pid, "yt")["status"], "failed")
        self.assertEqual(self.bot.said[-1][1]["inline_keyboard"][0][0]["callback_data"], f"ytretry:{self.pid}")

    def test_lost_response_resumes_session_instead_of_new_upload(self):
        self.login()
        self.db.set(f"yt_session:{self.pid}", "https://upload.example/session1")
        calls = []

        def put(url, data=None, headers=None, timeout=None):
            calls.append(headers.get("Content-Range"))
            return R(200, {"id": "abc123"})   # YouTube уже всё получил
        with mock.patch("engine.youtube.requests.put", side_effect=put):
            self.db.queue_crosspost(self.pid, "yt", "2026-01-01T00:00:00+00:00")
            self.engine.job_crosspost()
        self.assertEqual(calls, ["bytes */2048"])
        self.assertIsNone(self.g.meta)      # новая загрузка не начиналась
        self.assertEqual(self.db.crosspost(self.pid, "yt")["external_id"], "abc123")

    def test_private_visibility_is_reported(self):
        self.login()
        orig = self.g.request

        def req(method, url, params=None, headers=None, timeout=None):
            if url.endswith("/videos") and params.get("part") == "status":
                return R(200, {"items": [{"status": {"privacyStatus": "private", "uploadStatus": "uploaded"}}]})
            return orig(method, url, params=params, headers=headers, timeout=timeout)
        with mock.patch("engine.youtube.requests.request", side_effect=req):
            url = self.engine.yt.publish(self.pid)
        self.assertIn("аудит YouTube API", url)

    def test_callback_escapes_error_text(self):
        self.engine.yt.start_login()
        state = self.db.get("yt_state")
        page = self.engine.yt_callback({"state": state, "error": "x", "error_description": "<script>alert(1)</script>"})
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        page = self.engine.yt_callback({"state": "чужой", "error": "x", "error_description": "<b>фишинг</b>"})
        self.assertNotIn("фишинг", page)

    def test_expired_token_is_refreshed(self):
        self.login()
        self.db.set("yt_token_expires", "2000-01-01T00:00:00+00:00")
        self.assertEqual(self.engine.yt.token, "ya29.T2")
        self.assertEqual(self.g.token_calls[-1]["grant_type"], "refresh_token")

    def test_not_configured(self):
        self.cfg.yt_client_id = ""
        self.assertEqual(self.engine.on_approved(self.pid, now=True), "")
        self.assertIn("YT_CLIENT_ID", self.engine.yt.status())


class LegalPagesTests(unittest.TestCase):
    def test_pages_for_google_branding(self):
        from engine import server
        pages = server.legal_pages("@Easyfotobot")
        self.assertEqual(set(pages), {"/home", "/privacy", "/terms"})
        self.assertIn("не передаются третьим лицам", pages["/privacy"]({}))
        self.assertIn("t.me/Easyfotobot", pages["/home"]({}))


if __name__ == "__main__":
    unittest.main()
