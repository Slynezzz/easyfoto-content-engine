import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from engine import analyst, attribution
from engine.config import Config
from engine.db import DB

UTC = timezone.utc


def u(source, campaign="", at="2026-10-20 12:30:00", photo=False, bought=False, medium="bio"):
    return {"user_id": 1, "source": source, "medium": medium, "campaign": campaign,
            "registered_at": datetime.fromisoformat(at), "photo": photo, "bought": bought}


class AttributionTests(unittest.TestCase):
    def test_exact_campaign_and_time_window_split(self):
        pubs = {"ig": [(1, datetime(2026, 10, 20, 9, tzinfo=UTC)), (2, datetime(2026, 10, 19, 16, tzinfo=UTC)),
                       (3, datetime(2026, 10, 17, 9, tzinfo=UTC))],  # #3 вышел раньше окна в 48 ч
                "vk": [], "yt": []}
        users = [u("vk", "p7", photo=True, bought=True, medium="clip"),   # точно ролику #7
                 u("ig", photo=True),                                     # поровну #1 и #2
                 u("tg"),                                                 # нет роликов в TG — не учитываем
                 u("ig", at="2026-10-20 08:00:00")]                       # до выхода #1 — только #2
        conv = attribution.attribute(users, pubs)
        self.assertEqual(conv[7]["points"], 9.0)
        self.assertAlmostEqual(conv[1]["points"], 1.5)
        self.assertAlmostEqual(conv[2]["points"], 2.5)
        self.assertNotIn(3, conv)
        self.assertIn("купили", attribution.summary(conv))

    def test_publications_from_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "e.db")
            a = db.create_post(status="published", format="f", hook_id="h", theme="t", script_json={},
                               published_at="2026-10-20T09:00:00+00:00")
            db.queue_crosspost(a, "yt", "2026-10-20T09:00:00+00:00")
            db.update_crosspost(a, "yt", status="published")
            db.queue_crosspost(a, "vk_manual", "2026-10-20T09:00:00+00:00")
            pubs = attribution.publications(db)
            db.close()
        self.assertEqual([p for p, _ in pubs["ig"]], [a])
        self.assertEqual([p for p, _ in pubs["yt"]], [a])
        self.assertEqual(pubs["vk"], [])  # ещё не выложен


class ScoringTests(unittest.TestCase):
    rows = [{"post_id": i, "plays": 1000, "skip_rate": 50, "saves": 5, "shares": 5, "comments": 5,
             "avg_watch_ms": 5000, "format": "f", "hook_id": "h", "theme": "t"} for i in (1, 2)]

    def test_conversion_changes_the_winner(self):
        same = analyst.score_posts([dict(r) for r in self.rows])
        self.assertEqual(same[0]["score"], same[1]["score"])
        scored = analyst.score_posts([dict(r) for r in self.rows],
                                     {1: {"points": 0.0}, 2: {"points": 9.0, "regs": 1, "bought": 1}})
        self.assertGreater(scored[1]["score"], scored[0]["score"])
        self.assertEqual(scored[1]["regs"], 1)

    def test_engagement_uses_instagram_plays_only(self):
        rows = [dict(r) for r in self.rows]
        rows[1]["ig_plays"], rows[1]["plays"] = 1000, 5000   # +4000 просмотров Shorts
        scored = analyst.score_posts(rows)
        self.assertAlmostEqual(scored[1]["engagement_rate"], 0.015)

    def test_posts_outside_window_get_middle_rank(self):
        rows = [dict(r) for r in self.rows] + [dict(self.rows[0], post_id=3)]
        conv = {1: {"points": 0.0}, 2: {"points": 9.0}}   # #3 — старый, вне окна
        sc = {r["post_id"]: r["score"] for r in analyst.score_posts(rows, conv)}
        self.assertGreater(sc[2], sc[3])
        self.assertGreater(sc[3], sc[1])

    def test_no_conversions_keeps_old_weights(self):
        a = analyst.score_posts([dict(r) for r in self.rows])
        b = analyst.score_posts([dict(r) for r in self.rows], {9: {"points": 3.0}})  # ролик вне выборки
        self.assertEqual([r["score"] for r in a], [r["score"] for r in b])

    def test_conversions_without_db_or_on_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "e.db")
            self.assertEqual(analyst.conversions(Config(bot_database_url=""), db), ({}, ""))
            from engine import funnel
            with mock.patch.object(funnel, "users", side_effect=funnel.FunnelError("нет связи")):
                conv, text = analyst.conversions(Config(bot_database_url="postgresql://a:b@h:1/r"), db)
            db.close()
        self.assertEqual(conv, {})
        self.assertIn("не учтены", text)


if __name__ == "__main__":
    unittest.main()
