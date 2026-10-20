import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from engine import balances, planner, vk
from engine.app import Engine
from engine.config import Config
from engine.db import DB
from tests.test_engine import TZ, Clock, FakeBot, FakeIG

TOKEN_URL = "https://oauth.vk.com/blank.html#access_token=vk1.a.SECRET&expires_in=0&user_id=42"


class R:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        if isinstance(self._d, Exception):
            raise self._d
        return self._d


class VKApi:
    """Подделка VK: запоминает вызовы методов и загрузку."""

    def __init__(self, fail_wall=False):
        self.calls, self.fail_wall, self.uploads = [], fail_wall, 0

    def post(self, url, data=None, files=None, timeout=None):
        if files:
            self.uploads += 1
            return R({"video_id": 456, "owner_id": -777})
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, data))
        if method == "users.get":
            return R({"response": [{"first_name": "Максим", "last_name": "К"}]})
        if method == "groups.getById":
            return R({"response": {"groups": [{"name": "Нейрофото", "is_admin": 1}]}})
        if method == "video.save":
            return R({"response": {"upload_url": "https://upload.vk/x", "video_id": 456, "owner_id": -777}})
        if method == "wall.post":
            if self.fail_wall:
                return R({"error": {"error_code": 214, "error_msg": "Access to adding post denied"}})
            return R({"response": {"post_id": 99}})
        return R({"error": {"error_code": 3, "error_msg": "Unknown method"}})


class VKTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), publish_hours="12,19", reels_per_day=2,
                          vk_app_id="123", vk_group_id=777, vk_access_token="", max_bot_url="https://max.ru/bot")
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.clock = Clock(datetime(2026, 10, 20, 9, 0, tzinfo=TZ))
        self.bot = FakeBot()
        self.engine = Engine(self.cfg, self.db, ai=None, ig=FakeIG(self.db, ready=False), bot=self.bot, now=self.clock)
        self.engine.vk.sleep = lambda s: None
        video = Path(self._tmp.name) / "v.mp4"
        video.write_bytes(b"\x00" * 100)
        self.pid = self.db.create_post(status="approved", format="worlds", hook_id="h", theme="t",
                                       script_json={"script": {"hook": "Хотел *фото* на Хэллоуин"}},
                                       video_path=str(video), caption="Текст поста #нейрофото",
                                       scheduled_for="2026-10-20 12:00")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def connect(self, api):
        with mock.patch("engine.vk.requests.post", side_effect=api.post):
            return self.engine.vk.save_token(TOKEN_URL)

    def test_parse_token_and_connect(self):
        self.assertEqual(vk.parse_token_url(TOKEN_URL), ("vk1.a.SECRET", 0, "42"))
        self.assertIsNone(vk.parse_token_url("привет"))
        msg = self.connect(VKApi())
        self.assertIn("Максим", msg)
        self.assertIn("бессрочный", msg)
        self.assertIn("Нейрофото", msg)
        self.assertTrue(self.engine.vk.ready())
        self.assertIn("scope=video,wall,groups,offline", vk.legacy_auth_url(self.cfg))

    def test_vk_id_login_exchange_and_auto_refresh(self):
        self.cfg.public_base_url = "https://engine.up.railway.app"
        link = self.engine.vk.start_login()
        q = {k: v[0] for k, v in __import__("urllib.parse").parse.parse_qs(link.split("?", 1)[1]).items()}
        self.assertTrue(link.startswith("https://id.vk.ru/authorize?"))
        self.assertEqual(q["redirect_uri"], "https://engine.up.railway.app/vk/callback")
        self.assertEqual((q["code_challenge_method"], q["scope"]), ("S256", "video wall groups"))
        oauth = []

        def post(url, data=None, files=None, timeout=None):
            if "oauth2/auth" in url:
                oauth.append(dict(data))
                n = len(oauth)
                return R({"access_token": f"vk2.a.T{n}", "refresh_token": f"R{n}", "expires_in": 3600,
                          "user_id": 42, "scope": "video wall groups"})
            return VKApi().post(url, data=data, files=files, timeout=timeout)
        with mock.patch("engine.vk.requests.post", side_effect=post):
            with self.assertRaises(vk.VKError):
                self.engine.vk.finish_login({"code": "c", "state": "чужой", "device_id": "d"})
            msg = self.engine.vk.finish_login({"code": "c", "state": q["state"], "device_id": "dev1"})
            self.assertIn("✅ VK подключён (VK ID): Максим", msg)
            self.assertNotIn("не хватает", msg)
            ex = oauth[0]
            self.assertEqual((ex["grant_type"], ex["device_id"], ex["client_id"]), ("authorization_code", "dev1", "123"))
            self.assertEqual(self.engine.vk.token, "vk2.a.T1")      # ключ свежий — без продления
            self.db.set("vk_token_expires", "2000-01-01T00:00:00+00:00")
            self.assertEqual(self.engine.vk.token, "vk2.a.T2")      # истёк — продлили сами
            self.assertEqual((oauth[1]["grant_type"], oauth[1]["refresh_token"]), ("refresh_token", "R1"))
            # ссылка входа одноразовая
            with self.assertRaises(vk.VKError):
                self.engine.vk.finish_login({"code": "c", "state": q["state"], "device_id": "dev1"})
        self.assertIn("VK ID", self.engine.vk.status())

    def test_refresh_failure_is_reported(self):
        self.cfg.vk_manual = False
        self.db.set("vk_refresh_token", "R")
        self.db.set("vk_token_expires", "2000-01-01T00:00:00+00:00")
        with mock.patch("engine.vk.requests.post", return_value=R({"error": "invalid_grant"})):
            warn = self.engine.vk.token_warning()
        self.assertIn("не удалось продлить", warn)
        self.assertIn("не продлевается", self.engine.vk.status())

    def test_approval_queues_vk_at_slot_and_publishes_with_bot_link(self):
        api = VKApi()
        self.connect(api)
        self.assertEqual(self.engine.on_approved(self.pid, now=False), "📘 VK: 20.10 12:00")
        self.assertEqual(self.engine.on_approved(self.pid, now=False), "")  # без дублей
        with mock.patch("engine.vk.requests.post", side_effect=api.post):
            self.assertEqual(self.engine.job_crosspost(), 0)  # время не пришло
            self.clock.dt = datetime(2026, 10, 20, 12, 1, tzinfo=TZ)
            self.assertEqual(self.engine.job_crosspost(), 1)
        save = next(d for m, d in api.calls if m == "video.save")
        wall = next(d for m, d in api.calls if m == "wall.post")
        self.assertEqual(save["group_id"], 777)
        self.assertEqual(save["name"], "Хотел фото на Хэллоуин")
        self.assertIn("https://max.ru/bot?start=utm_vk_clip_p1", wall["message"])
        self.assertEqual(wall["attachments"], "video-777_456")
        self.assertEqual(wall["owner_id"], -777)
        row = self.db.crosspost(self.pid, "vk")
        self.assertEqual((row["status"], row["url"]), ("published", "https://vk.com/wall-777_99"))
        self.assertIn("опубликован", self.bot.said[-1][0])

    def test_failure_offers_retry_without_second_upload(self):
        api = VKApi(fail_wall=True)
        self.connect(api)
        self.engine.on_approved(self.pid, now=True)
        with mock.patch("engine.vk.requests.post", side_effect=api.post):
            self.engine.job_crosspost()
            self.assertEqual(self.db.crosspost(self.pid, "vk")["status"], "failed")
            self.assertIn("запрещена настройками", self.bot.said[-1][0])
            self.assertEqual(self.bot.said[-1][1]["inline_keyboard"][0][0]["callback_data"], f"vkretry:{self.pid}")
            api.fail_wall = False
            self.engine.vk_retry(self.pid)
            self.engine.job_crosspost()
        self.assertEqual(api.uploads, 1)   # видео загружено один раз
        self.assertEqual(self.db.crosspost(self.pid, "vk")["status"], "published")

    def test_not_connected_does_nothing(self):
        self.cfg.vk_manual = False
        self.assertEqual(self.engine.on_approved(self.pid, now=True), "")
        self.assertIn("нужен вход — /vk", self.engine.vk.status())

    def test_manual_mode_without_video_rights(self):
        # VK ID выдал только личные данные — автопубликация невозможна, работает полуавтомат
        self.db.set("vk_refresh_token", "R")
        self.db.set("vk_scope_granted", "vkid.personal_info")
        self.assertFalse(self.engine.vk.can_autopublish())
        self.assertEqual(self.engine.on_approved(self.pid, now=False), "📘 VK (пришлю файл и текст): 20.10 12:00")
        self.assertEqual(self.engine.job_crosspost(), 0)
        self.clock.dt = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
        self.assertEqual(self.engine.job_crosspost(), 1)
        pid, text = self.bot.vk_packages[0]
        self.assertEqual(pid, self.pid)
        self.assertIn("https://max.ru/bot?start=utm_vk_clip_p1", text)
        self.assertEqual(self.db.crosspost(self.pid, "vk_manual")["status"], "sent")
        self.engine.job_crosspost()
        self.assertEqual(len(self.bot.vk_packages), 1)   # повторно не шлёт
        self.assertIn("полуавтомат", self.engine.vk.status())

    def test_restart_mid_publish_turns_to_failed(self):
        self.db.queue_crosspost(self.pid, "vk", "2026-10-20T00:00:00+00:00")
        self.db.claim_crosspost(self.pid, "vk", "queued", "publishing")
        self.engine.recover()
        self.assertEqual(self.db.crosspost(self.pid, "vk")["status"], "failed")

    def test_token_expiry_warning(self):
        self.db.set("vk_token", "t")
        soon = (datetime.now(ZoneInfo("UTC")) + timedelta(hours=5)).isoformat(timespec="seconds")
        self.db.set("vk_token_expires", soon)
        self.assertIn("истечёт", self.engine.vk.token_warning())
        self.assertTrue(self.engine.job_vk_token())
        n = len(self.bot.said)
        self.engine.job_vk_token()
        self.assertEqual(len(self.bot.said), n)  # не повторяет


class BalanceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self._tmp.name) / "e.db")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_new_api_token_balance_and_warning(self):
        def get(url, headers=None, params=None, timeout=None):
            if url.endswith("/api/usage/token"):
                return R({"data": {"total_available": 1_000_000, "unlimited_quota": False}})
            return R({}, 404)
        with mock.patch("engine.balances.requests.get", side_effect=get):
            b = balances.gateway("SaintStore", "https://saint.example", "k", 3.0)
        self.assertEqual(b.text, "осталось ≈ $2.00")
        self.assertIn("$2.00", b.warn)

    def test_openai_billing_format_and_unknown(self):
        def get(url, headers=None, params=None, timeout=None):
            if url.endswith("/billing/subscription"):
                return R({"hard_limit_usd": 20})
            if url.endswith("/billing/usage"):
                return R({"total_usage": 550})
            return R(ValueError("html"))
        with mock.patch("engine.balances.requests.get", side_effect=get):
            b = balances.gateway("AI STAR", "https://ai.star.example/v1", "k", 3.0)
        self.assertEqual((b.text, b.warn), ("осталось ≈ $14.50", ""))
        with mock.patch("engine.balances.requests.get", return_value=R(ValueError("html"))):
            b = balances.gateway("X", "https://x.example", "k", 3.0)
        self.assertIn("не сообщает", b.text)

    def test_elevenlabs_and_tavily(self):
        cfg = Config(data_dir=Path(self._tmp.name), music_api_key="xi", search_provider="tavily",
                     search_api_key="tvly", gemini_api_key="", openai_api_key="", image_api_key="")

        def get(url, headers=None, params=None, timeout=None):
            if "elevenlabs" in url:
                self.assertEqual(headers, {"xi-api-key": "xi"})
                return R({"character_count": 27000, "character_limit": 30000, "tier": "starter"})
            return R({"account": {"current_plan": "Researcher", "plan_usage": 100, "plan_limit": 1000}})
        with mock.patch("engine.balances.requests.get", side_effect=get):
            text = balances.report(cfg, self.db)
            warns = balances.warnings(cfg, self.db)
        self.assertIn("осталось 3 000 из 30 000 кредитов", text)
        self.assertIn("осталось 900 из 1000 запросов", text)
        self.assertEqual(len(warns), 1)
        self.assertIn("ElevenLabs", warns[0])

    def test_money_errors_are_flagged(self):
        self.db.set("last_music_error", "сервис музыки ответил 402: upgrade to a paid plan")
        self.db.set("last_ai_error", "")
        self.assertEqual(len(balances.recent_errors(self.db)), 1)


class CopyModelTests(unittest.TestCase):
    def test_strong_model_writes_and_falls_back(self):
        from engine import playbook as pbm
        pb = pbm.load(Path("/nonexistent/playbook.json"))
        choice = pbm.choose(pb, datetime(2026, 10, 20).date(), 0.0, __import__("random").Random(1))
        ai = mock.Mock()
        calls = []

        def fake_json(system, prompt, images=None, temperature=0.9, model=None):
            calls.append(model)
            if model:
                raise planner.AIError("Unknown model")
            raise planner.AIError("down")
        ai.json.side_effect = fake_json
        res = planner.plan(ai, pb, choice, 4, copy_model="claude-sonnet-5-5")
        self.assertEqual(calls, ["claude-sonnet-5-5", None, None])
        self.assertIn("claude-sonnet-5-5: Unknown model", res.copy_error)
        self.assertFalse(res.llm_ok)


if __name__ == "__main__":
    unittest.main()
