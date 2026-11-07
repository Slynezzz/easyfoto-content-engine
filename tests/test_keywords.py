import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine import funnel, keywords, leads
from engine.config import Config
from engine.db import DB


class FakeIG:
    def __init__(self, comments, fail_dm=False):
        self.comments, self.fail_dm, self.calls = comments, fail_dm, []
        self.account_id = "1784"

    def ready(self):
        return True

    def _req(self, method, path, **params):
        self.calls.append((method, path, params))
        if path == "1784/media":
            return {"data": [{"id": "M1", "comments_count": len(self.comments), "timestamp": ts()},
                             {"id": "M9", "comments_count": 0, "timestamp": ts()}]}
        if path.endswith("/comments"):
            return {"data": self.comments}
        if path.endswith("/messages") and self.fail_dm:
            raise RuntimeError("(#10) permission denied")
        return {"id": "x"}


def ts(days=0):
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+0000")


class KeywordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(data_dir=self.tmp, niche="expert", bot_username="slyness_bot", lead_bot_token="x",
                          admin_chat_id=111)
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.db.set("ig_username", "max.karpoff")
        self.pid = self.db.create_post(status="published", format="post", hook_id="h", theme="t",
                                       script_json="{}", ig_media_id="M1",
                                       published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def test_add_parse_find_remove(self):
        self.assertEqual(keywords.parse_command(["бот"]), ("бот", "", "magnet", ""))
        self.assertEqual(keywords.parse_command(["ГАЙД", "Гайд", "по", "ботам", "|", "https://x.ru/g"]),
                         ("ГАЙД", "Гайд по ботам", "link", "https://x.ru/g"))
        kid = keywords.add(self.db, "бот")
        self.assertEqual(keywords.add(self.db, "БОТ", "Чек-лист 12 задач"), kid)   # то же слово — обновление
        self.assertEqual(keywords.find_in(self.db, "Хочу! бот, пожалуйста")["id"], kid)
        self.assertIsNone(keywords.find_in(self.db, "работа без ботов"))            # только отдельным словом
        keywords.add(self.db, "ЁЛКА", "Новогодний", "text", "Поздравляем")
        self.assertIsNotNone(keywords.find_in(self.db, "елка"))
        with self.assertRaises(keywords.KeywordError):
            keywords.add(self.db, "два слова")
        with self.assertRaises(keywords.KeywordError):
            keywords.add(self.db, "ГАЙД", "", "link", "javascript:alert(1)")
        self.assertTrue(keywords.remove(self.db, "бот"))
        self.assertFalse(keywords.remove(self.db, "бот"))

    def test_scan_replies_once_and_skips_own_and_old(self):
        kid = keywords.add(self.db, "БОТ", "Чек-лист")
        ig = FakeIG([{"id": "c1", "text": "БОТ", "username": "anna", "timestamp": ts()},
                     {"id": "c2", "text": "бот", "username": "max.karpoff", "timestamp": ts()},
                     {"id": "c3", "text": "бот", "username": "old", "timestamp": ts(days=9)},
                     {"id": "c4", "text": "класс", "username": "ivan", "timestamp": ts()}])
        res = keywords.scan(self.cfg, self.db, ig)
        self.assertEqual(res["replied"], 1)
        dm = [c for c in ig.calls if c[1].endswith("/messages")]
        self.assertEqual(len(dm), 1)
        self.assertEqual(json.loads(dm[0][2]["recipient"]), {"comment_id": "c1"})
        text = json.loads(dm[0][2]["message"])["text"]
        self.assertIn(f"https://t.me/slyness_bot?start=utm_ig_kw_k{kid}p{self.pid}", text)
        self.assertTrue(any(c[1] == "c1/replies" for c in ig.calls))
        self.assertEqual(keywords.get(self.db, kid)["uses"], 1)
        self.assertFalse([c for c in ig.calls if c[1] == "M9/comments"])     # без комментариев — не читаем
        ig.calls.clear()
        self.assertEqual(keywords.scan(self.cfg, self.db, ig)["replied"], 0)    # повторно не отвечаем
        self.assertEqual([c[1] for c in ig.calls], ["1784/media"])              # новых нет — один запрос
        ig.comments.append({"id": "c5", "text": "БОТ!", "username": "olga", "timestamp": ts()})
        self.assertEqual(keywords.scan(self.cfg, self.db, ig)["replied"], 1)

    def test_scan_reports_permission_errors(self):
        keywords.add(self.db, "БОТ")
        res = keywords.scan(self.cfg, self.db, FakeIG([{"id": "c1", "text": "бот", "username": "a",
                                                         "timestamp": ts()}], fail_dm=True))
        self.assertEqual(res["replied"], 0)
        self.assertIn("permission", res["errors"][0])

    def test_link_and_text_dm(self):
        k = keywords.get(self.db, keywords.add(self.db, "ГАЙД", "Гайд", "link", "https://x.ru/g"))
        self.assertIn("https://x.ru/g", keywords.dm_text(self.cfg, k))

    def test_start_param_and_lead_bot_delivery(self):
        kid = keywords.add(self.db, "РАЗБОР", "Шаблон ТЗ", "text", "Вот шаблон ТЗ: …")
        p = funnel.parse_start(f"utm_ig_kw_k{kid}p{self.pid}")
        self.assertEqual((p["source"], p["keyword_id"], p["post_id"]), ("ig", kid, self.pid))
        api = mock.Mock()
        api.send_message.return_value = {"message_id": 1}
        bot = leads.LeadBot(self.cfg, self.db, api)
        bot.handle({"message": {"message_id": 1, "chat": {"id": 5, "type": "private"},
                                "from": {"id": 5, "first_name": "Анна"}, "text": f"/start utm_ig_kw_k{kid}p{self.pid}"}})
        texts = [c.args[1] for c in api.send_message.call_args_list]
        self.assertIn("Вот шаблон ТЗ: …", texts)
        row = leads.get(self.db, 5)
        self.assertEqual((row["post_id"], row["source"]), (self.pid, "ig"))
        self.assertIsNotNone(row["magnet_at"])
        # слово прямо в бот — тоже выдаёт
        api.reset_mock()
        bot.handle({"message": {"message_id": 2, "chat": {"id": 5, "type": "private"},
                                "from": {"id": 5, "first_name": "Анна"}, "text": "разбор"}})
        self.assertIn("Вот шаблон ТЗ: …", [c.args[1] for c in api.send_message.call_args_list])

    def test_admin_commands(self):
        from engine.admin_bot import AdminBot, Services
        api = mock.Mock()
        api.send_message.return_value = {"message_id": 1}
        bot = AdminBot(self.cfg, self.db, api, Services(make_one=lambda: 0, instagram_ready=lambda: False,
                                                        run_analysis=lambda: "", stats_text=lambda: ""))
        bot.on_command("/keyword", ["ГАЙД", "Гайд", "|", "https://x.ru/g"])
        self.assertEqual(keywords.find_in(self.db, "гайд")["kind"], "link")
        bot.on_command("/keywords", [])
        kb = api.send_message.call_args.args[2]
        self.assertTrue(kb["inline_keyboard"][0][0]["callback_data"].startswith("kwdel:"))
        bot.on_callback({"id": "c", "data": kb["inline_keyboard"][0][0]["callback_data"], "message": {}})
        self.assertIsNone(keywords.find_in(self.db, "гайд"))


if __name__ == "__main__":
    unittest.main()


class TelegramMediaGroupTests(unittest.TestCase):
    def test_caption_string_and_documents(self):
        from engine.telegram import TelegramAPI
        tmp = Path(tempfile.mkdtemp())
        paths = []
        for i in range(3):
            p = tmp / f"{i}.jpg"
            p.write_bytes(b"x")
            paths.append(p)
        api = TelegramAPI("T")
        with mock.patch.object(api, "call", return_value=[{"message_id": 1}]) as call:
            api.send_media_group(1, paths, "Подпись поста")
            media = call.call_args.kwargs["media"]
            self.assertEqual(media[0]["caption"], "Подпись поста")
            self.assertNotIn("caption", media[1])
            api.send_media_group(1, paths, as_documents=True)
            self.assertEqual({m["type"] for m in call.call_args.kwargs["media"]}, {"document"})
            api.send_media_group(1, paths, ["a", "b"])
            self.assertEqual([m.get("caption") for m in call.call_args.kwargs["media"]], ["a", "b", None])


class SafeUrlTests(unittest.TestCase):
    def test_blocks_internal_addresses(self):
        from engine import websearch
        for bad in ("http://127.0.0.1/", "http://localhost:8080/", "http://10.0.0.5/x", "file:///etc/passwd",
                    "http://postgres.railway.internal:5432", "http://169.254.169.254/latest/meta-data",
                    "ftp://example.com/", "javascript:alert(1)"):
            self.assertFalse(websearch.safe_url(bad), bad)
            self.assertEqual(websearch.page_text(bad), "")
