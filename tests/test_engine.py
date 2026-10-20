import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from engine import analyst, playbook as pbm
from engine.app import SLOT_FMT, Engine
from engine.config import Config
from engine.db import DB
from engine.instagram import IGError

TZ = ZoneInfo("Europe/Moscow")


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


class FakeIG:
    def __init__(self, db, ready=True, fail=False):
        self.db, self._ready, self.fail, self.published = db, ready, fail, []

    def ready(self):
        return self._ready

    def publish(self, pid):
        if self.fail:
            self.db.update_post(pid, status="failed")
            raise IGError("boom")
        self.db.update_post(pid, status="published")
        self.published.append(pid)
        return f"https://instagram.com/reel/{pid}"

    def collect_all(self):
        return 0

    def refresh_token(self):
        return True


class FakeBot:
    def __init__(self):
        self.said, self.sent = [], []

    def say(self, text, markup=None):
        self.said.append((text, markup))

    def send_for_approval(self, pid):
        self.sent.append(pid)
        return True

    def push_rendered(self):
        return 0


class EngineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), publish_hours="12,19", reels_per_day=2, plan_hour=8)
        self.db = DB(self.cfg.db_path)
        self.clock = Clock(datetime(2026, 10, 20, 9, 0, tzinfo=TZ))
        self.ig = FakeIG(self.db)
        self.bot = FakeBot()
        self.engine = Engine(self.cfg, self.db, ai=None, ig=self.ig, bot=self.bot, now=self.clock)
        self.made = []

        def fake_make(scheduled_for=None, occasion=None):
            pid = self.db.create_post(status="rendered", format="worlds", hook_id="eras",
                                      theme=occasion.theme_id if occasion else "eras",
                                      script_json={}, scheduled_for=scheduled_for)
            self.made.append((pid, scheduled_for))
            return pid
        self.engine.make_one = fake_make

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_plan_fills_todays_future_slots_once(self):
        self.assertEqual(self.engine.job_plan(), 2)
        self.assertEqual([s for _, s in self.made], ["2026-10-20 12:00", "2026-10-20 19:00"])
        self.assertEqual(self.bot.sent, [pid for pid, _ in self.made])
        self.assertEqual(self.engine.job_plan(), 0)  # повторно не собирает

    def test_plan_waits_for_plan_hour_and_skips_past_slots(self):
        self.clock.dt = datetime(2026, 10, 20, 7, 0, tzinfo=TZ)
        self.assertEqual(self.engine.job_plan(), 0)
        self.clock.dt = datetime(2026, 10, 20, 15, 0, tzinfo=TZ)  # сервис стартовал днём
        self.assertEqual(self.engine.job_plan(), 1)
        self.assertEqual(self.made[0][1], "2026-10-20 19:00")

    def test_rejected_slot_is_not_refilled(self):
        self.engine.job_plan()
        self.db.update_post(self.made[0][0], status="rejected")
        self.assertEqual(self.engine.job_plan(), 0)

    def test_plan_failure_notifies_with_hint(self):
        self.engine.make_one = mock.Mock(side_effect=RuntimeError("нет фото"))
        self.engine.job_plan()
        self.assertTrue(any("нет фото" in t and "/make" in t for t, _ in self.bot.said))

    def test_publish_due_only_and_one_at_a_time(self):
        a = self.db.create_post(status="approved", format="w", hook_id="h", theme="t", script_json={},
                                scheduled_for="2026-10-20 08:00")
        b = self.db.create_post(status="approved", format="w", hook_id="h", theme="t", script_json={},
                                scheduled_for="2026-10-20 08:30")
        c = self.db.create_post(status="approved", format="w", hook_id="h", theme="t", script_json={},
                                scheduled_for="2026-10-20 19:00")
        self.assertEqual(self.engine.job_publish(), 1)
        self.assertEqual(self.engine.job_publish(), 1)
        self.assertEqual(self.engine.job_publish(), 0)
        self.assertEqual(self.ig.published, [a, b])
        self.assertEqual(self.db.post(c)["status"], "approved")

    def test_unscheduled_approved_gets_next_free_slot(self):
        self.db.create_post(status="pending", format="w", hook_id="h", theme="t", script_json={},
                            scheduled_for="2026-10-20 12:00")
        pid = self.db.create_post(status="approved", format="w", hook_id="h", theme="t", script_json={})
        self.engine.job_publish()
        self.assertEqual(self.db.post(pid)["scheduled_for"], "2026-10-20 19:00")

    def test_pause_and_instagram_not_ready_block_publishing(self):
        self.db.create_post(status="approved", format="w", hook_id="h", theme="t", script_json={},
                            scheduled_for="2026-10-20 08:00")
        self.db.set("paused", "1")
        self.assertEqual(self.engine.job_publish(), 0)
        self.db.set("paused", "0")
        self.ig._ready = False
        self.assertEqual(self.engine.job_publish(), 0)

    def test_publish_failure_offers_retry_button(self):
        self.ig.fail = True
        pid = self.db.create_post(status="approved", format="w", hook_id="h", theme="t", script_json={},
                                  scheduled_for="2026-10-20 08:00")
        self.engine.job_publish()
        markups = [m for _, m in self.bot.said if m]
        self.assertEqual(markups[0]["inline_keyboard"][0][0]["callback_data"], f"retry:{pid}")

    def test_analysis_runs_once_per_week_on_monday(self):
        with mock.patch("engine.app.analyst.run", return_value="отчёт") as run:
            self.clock.dt = datetime(2026, 10, 19, 11, 0, tzinfo=TZ)  # понедельник
            self.assertTrue(self.engine.job_analysis())
            self.assertFalse(self.engine.job_analysis())
            self.clock.dt = datetime(2026, 10, 21, 11, 0, tzinfo=TZ)  # среда
            self.assertFalse(self.engine.job_analysis())
            self.clock.dt = datetime(2026, 10, 26, 11, 0, tzinfo=TZ)
            self.assertTrue(self.engine.job_analysis())
        self.assertEqual(run.call_count, 2)

    def test_recover_releases_stuck_posts(self):
        a = self.db.create_post(status="publishing", format="w", hook_id="h", theme="t", script_json={})
        b = self.db.create_post(status="sending", format="w", hook_id="h", theme="t", script_json={})
        self.assertEqual(self.engine.recover(), 2)
        self.assertEqual(self.db.post(a)["status"], "failed")
        self.assertEqual(self.db.post(b)["status"], "rendered")
        self.assertEqual(self.bot.said[-1][1]["inline_keyboard"][0][0]["callback_data"], f"retry:{a}")

    def test_cleanup_removes_only_finished_media(self):
        files = {}
        for name, status, pub in (("rej", "rejected", None), ("old", "published", "2026-10-01T00:00:00+00:00"),
                                  ("new", "published", "2026-10-19T00:00:00+00:00"), ("wait", "pending", None)):
            v = Path(self._tmp.name) / f"{name}.mp4"
            v.write_bytes(b"x")
            files[name] = v
            self.db.create_post(status=status, format="w", hook_id="h", theme="t", script_json={},
                                video_path=str(v), published_at=pub)
        self.assertEqual(self.engine.job_cleanup(), 2)
        self.assertEqual({k for k, v in files.items() if v.exists()}, {"new", "wait"})

    def test_fresh_token_is_not_refreshed_immediately(self):
        self.ig.refresh_token = mock.Mock(return_value=True)
        self.assertFalse(self.engine.job_token())
        self.ig.refresh_token.assert_not_called()
        self.assertTrue(self.engine.job_token())

    def test_tagging_error_is_reported_once(self):
        self.cfg.gemini_api_key = "k"
        err = __import__("engine.images", fromlist=["TaggingError"]).TaggingError("не получилось разметить фото: bad key")
        with mock.patch("engine.app.images.tag_library", side_effect=err), \
             mock.patch("engine.app.images.scan"):
            self.engine.job_tag()
            self.engine.job_tag()
        self.assertEqual(sum(1 for t, _ in self.bot.said if "bad key" in t), 1)

    def test_recover_resets_burned_tag_attempts_once(self):
        aid = self.db.add_asset("/x.jpg", "library")
        self.db.x("UPDATE assets SET tag_attempts=3 WHERE id=?", (aid,))
        self.engine.recover()
        self.assertEqual(self.db.one("SELECT tag_attempts FROM assets WHERE id=?", (aid,))["tag_attempts"], 0)
        self.db.x("UPDATE assets SET tag_attempts=3 WHERE id=?", (aid,))
        self.engine.recover()
        self.assertEqual(self.db.one("SELECT tag_attempts FROM assets WHERE id=?", (aid,))["tag_attempts"], 3)

    def test_tick_survives_failing_job(self):
        self.engine.job_tag = mock.Mock(side_effect=RuntimeError("x"))
        self.engine.tick()
        self.assertTrue(any("tag" in t for t, _ in self.bot.said))
        self.assertEqual(len(self.made), 2)  # остальные задачи отработали


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name))
        self.db = DB(self.cfg.db_path)
        pbm.load(self.cfg.playbook_path)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _published(self, fmt, hook, theme, plays, skip, saves, days_ago=5):
        pub = (datetime.now(TZ) - timedelta(days=days_ago)).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        pid = self.db.create_post(status="published", format=fmt, hook_id=hook, theme=theme, script_json={},
                                  ig_media_id=f"M{plays}", published_at=pub)
        self.db.add_metrics(pid, {"plays": plays, "reach": plays, "likes": 1, "comments": 0, "saves": saves,
                                  "shares": 0, "avg_watch_ms": 3000, "skip_rate": skip})
        return pid

    def test_not_enough_data(self):
        self._published("worlds", "eras", "eras", 100, 50, 1)
        self.assertIn("мало данных", analyst.run(self.cfg, self.db, None))

    def test_fresh_posts_are_ignored(self):
        for i in range(8):
            self._published("worlds", "eras", "eras", 100 + i, 50, 1, days_ago=1)
        self.assertIn("мало данных", analyst.run(self.cfg, self.db, None))

    def test_winning_format_and_hook_gain_weight(self):
        for i in range(4):
            self._published("before_after", "sent_selfies", "cinema", 5000 + i, 30, 50)
            self._published("worlds", "eras", "eras", 200 + i, 70, 1)

        class AI:
            def json(self, system, prompt, images=None, temperature=0.9):
                assert "before_after" in prompt
                return {"insights": ["Было→стало заходит лучше"], "summary": "Ставка на до/после."}

        report = analyst.run(self.cfg, self.db, AI())
        pb = pbm.load(self.cfg.playbook_path)
        self.assertGreater(pb["formats"]["before_after"]["weight"], 1.0)
        self.assertLess(pb["formats"]["worlds"]["weight"], 1.0)
        self.assertGreater(pb["hooks"]["sent_selfies"]["weight"], pb["hooks"]["eras"]["weight"])
        self.assertEqual(pb["insights"], ["Было→стало заходит лучше"])
        self.assertIn("▲", report)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM playbook_history")["n"], 1)

    def test_skip_rate_units_decided_by_whole_sample(self):
        self.assertEqual(analyst.skip_scale([0.35, 0.6]), 100)
        self.assertEqual(analyst.skip_scale([0.5, 35]), 1)   # 0.5% при процентах — не доля
        self.assertEqual(analyst.skip_pct(0.35, 100), 35)
        self.assertIsNone(analyst.skip_pct(None))

    def test_repeated_analysis_converges_instead_of_compounding(self):
        for i in range(4):
            self._published("before_after", "sent_selfies", "cinema", 5000 + i, 30, 50)
            self._published("worlds", "eras", "eras", 200 + i, 70, 1)
        weights = []
        for _ in range(8):
            analyst.run(self.cfg, self.db, None)
            weights.append(pbm.load(self.cfg.playbook_path)["formats"]["before_after"]["weight"])
        self.assertLess(weights[-1], 3.0)
        self.assertLess(abs(weights[-1] - weights[-2]), 0.02)

    def test_stats_text(self):
        self.assertIn("пока нет", analyst.stats_text(self.db))
        self._published("worlds", "eras", "eras", 1234, 40, 3)
        self.assertIn("1234", analyst.stats_text(self.db))


if __name__ == "__main__":
    unittest.main()
