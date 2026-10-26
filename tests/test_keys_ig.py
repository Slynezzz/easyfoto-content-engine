import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import balances
from engine.config import Config
from engine.db import DB
from engine.instagram import Instagram
from engine.llm import AI, KeyPool


class R:
    def __init__(self, status, data):
        self.status_code, self._d, self.text, self.headers, self.content = status, data, json.dumps(data), {}, b""

    def json(self):
        return self._d


class KeyPoolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / "e.db")
        self.cfg = Config(data_dir=Path(self.tmp.name), gemini_api_key="sk-cvc-AAAA1111",
                          gemini_api_keys="sk-cvc-BBBB2222, sk-cvc-CCCC3333", gemini_base_url="https://saint.example",
                          gemini_api_style="openai", openai_api_key="", image_api_key="")
        self.said = []
        self.sleep = mock.patch("engine.llm.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        self.db.close()
        self.tmp.cleanup()

    def test_exhausted_key_switches_and_is_remembered(self):
        ai = AI(self.cfg)
        ai.attach(self.db, self.said.append)
        used = []

        def post(url, headers=None, json=None, timeout=None, **kw):
            used.append(headers["Authorization"][-8:])
            if headers["Authorization"].endswith("AAAA1111"):
                return R(402, {"error": {"message": "Insufficient balance: token limit exhausted"}})
            return R(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})
        with mock.patch("engine.llm.requests.post", side_effect=post):
            self.assertEqual(ai.json("s", "p"), {"ok": True})
        self.assertEqual(used, ["AAAA1111", "BBBB2222"])
        self.assertEqual(self.cfg.gemini_api_key, "sk-cvc-BBBB2222")
        self.assertIn("переключился на …2222", self.said[-1])
        self.assertIn("осталось: 2", self.said[-1])
        # после перезапуска исчерпанный ключ не берётся первым
        cfg2 = Config(data_dir=Path(self.tmp.name), gemini_api_key="sk-cvc-AAAA1111",
                      gemini_api_keys="sk-cvc-BBBB2222", gemini_base_url="https://saint.example")
        ai2 = AI(cfg2)
        ai2.attach(self.db)
        self.assertEqual(cfg2.gemini_api_key, "sk-cvc-BBBB2222")

    def test_non_key_errors_do_not_burn_keys(self):
        ai = AI(self.cfg)
        ai.attach(self.db, self.said.append)
        with mock.patch("engine.llm.requests.post",
                        return_value=R(400, {"error": {"message": "bad request: messages malformed"}})):
            with self.assertRaises(Exception):
                ai.json("s", "p")
        self.assertEqual(self.cfg.gemini_api_key, "sk-cvc-AAAA1111")
        self.assertEqual(self.said, [])

    def test_all_keys_dead_warns_once_per_key(self):
        ai = AI(self.cfg)
        ai.attach(self.db, self.said.append)
        with mock.patch("engine.llm.requests.post",
                        return_value=R(401, {"error": {"message": "Invalid API key"}})):
            with self.assertRaises(Exception):
                ai.json("s", "p")
        self.assertIn("запасных рабочих ключей нет", self.said[-1])
        self.assertEqual(len(ai.pools["gemini"].working()), 0)

    def test_balance_report_probes_each_key(self):
        ai = AI(self.cfg)
        ai.attach(self.db)

        def post(url, headers=None, json=None, timeout=None):
            if headers["Authorization"].endswith("CCCC3333"):
                return R(402, {"error": {"message": "quota exceeded"}})
            return R(200, {"choices": [{"message": {"content": "o"}}]})
        with mock.patch("engine.balances.requests.post", side_effect=post), \
             mock.patch("engine.balances.requests.get", return_value=R(404, {})):
            text = balances.report(self.cfg, self.db, ai)
            warns = balances.warnings(self.cfg, self.db, ai)
        self.assertIn("SaintStore", text) if "saint" in self.cfg.gemini_base_url else None
        self.assertIn("1. …1111: ✅ работает ← сейчас", text)
        self.assertIn("3. …3333: ❌ исчерпан", text)
        self.assertEqual(warns, [])
        with mock.patch("engine.balances.requests.post",
                        return_value=R(402, {"error": {"message": "quota exceeded"}})), \
             mock.patch("engine.balances.requests.get", return_value=R(404, {})):
            warns = balances.warnings(self.cfg, self.db, ai)
        self.assertIn("ни один ключ не работает", warns[0])


class InstagramAccountTests(unittest.TestCase):
    def test_wrong_ig_user_id_is_replaced_by_token_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = DB(Path(tmp) / "e.db")
            cfg = Config(data_dir=Path(tmp), ig_user_id="2204726843410300", ig_access_token="IGAAtoken123456789",
                         public_base_url="https://e.example")
            ig = Instagram(cfg, db, sleep=lambda s: None)
            calls = []

            def get(url, params=None, timeout=None):
                calls.append(url)
                return R(200, {"user_id": "17841450687006314", "id": "999", "username": "maximum.ai",
                               "account_type": "MEDIA_CREATOR"})
            with mock.patch("engine.instagram.requests.get", side_effect=get):
                self.assertEqual(ig.account_id, "17841450687006314")
                self.assertEqual(ig.account_id, "17841450687006314")   # из базы, без запроса
                line = ig.check()
            self.assertEqual(len([c for c in calls if c.endswith("/me")]), 2)
            self.assertIn("@maximum.ai", line)
            self.assertIn("неверный номер", line)
            db.close()


if __name__ == "__main__":
    unittest.main()
