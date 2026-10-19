import json
import random
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from engine import playbook as pbm, scout
from engine.config import Config
from engine.db import DB
from engine.llm import AI, AIError
from engine.planner import _clip, plan
from engine.video import skins
from engine.video.templates import Script, looks_carousel


class SkinTests(unittest.TestCase):
    def test_tokens_and_marks(self):
        self.assertEqual(skins.tokens("Хочешь *так же?*"), [("Хочешь", False), ("так", True), ("же?", True)])
        self.assertEqual(skins.strip_marks("a *b c* d"), "a b c d")
        self.assertEqual(skins.tokens("стоп * звезда"), [("стоп", False), ("звезда", False)])

    def test_every_skin_renders_within_width(self):
        for name in skins.SKINS:
            with self.subTest(skin=name):
                img = skins.render("POV: фотосессии не было, а *фото есть* и ещё длинный хвост", name, 90)
                self.assertLessEqual(img.width, 900)
                self.assertGreater(img.height, 100)

    def test_reveal_keeps_layout(self):
        text = "Я в *разных эпохах* — где бы я выжил?"
        full = skins.render(text, "kinetic", 90)
        part = skins.render(text, "kinetic", 90, reveal=2)
        self.assertEqual(full.size, part.size)
        self.assertLess(part.getchannel("A").getbbox()[2], full.getchannel("A").getbbox()[2] + 1)
        empty = skins.render(text, "kinetic", 90, reveal=0)
        self.assertIsNone(empty.getchannel("A").getbbox())

    def test_dash_never_starts_a_line(self):
        font = skins._font(str(skins.FONT_BOLD), 90)
        words = skins.tokens("Я в разных эпохах — где бы я выжил")
        for avail in range(250, 900, 25):
            for ln in skins._layout(words, font, avail, font.getlength(" ")):
                self.assertNotEqual(words[ln[0]][0], "—", f"avail={avail}")

    def test_kinetic_hook_reveals_word_by_word(self):
        from tests.fixtures import make
        with tempfile.TemporaryDirectory() as d:
            imgs = [make(Path(d) / f"{i}.jpg", (600, 900), i) for i in range(3)]
            spec = looks_carousel(imgs, Script(hook="Раз два *три*", style="kinetic"))
            first = spec.segments[0].overlays
            hooks = [o for o in first if o.y < 700]
            self.assertEqual(len(hooks), 3)
            self.assertEqual([o.fade_out for o in hooks[:-1]], [False, False])
            # на остальных кадрах заголовок уже целиком
            self.assertEqual(len([o for o in spec.segments[1].overlays if o.y < 700]), 1)

    def test_clip_keeps_marks_balanced(self):
        self.assertEqual(_clip("Очень *длинное выделенное* слово тут", 12).count("*") % 2, 0)
        self.assertEqual(_clip("*вот это*", 20), "*вот это*")
        self.assertEqual(_clip("непарная * звезда", 30), "непарная  звезда".replace("  ", " "))


class PlaybookMigrationTests(unittest.TestCase):
    def test_old_playbook_gets_styles_and_keeps_weights(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pb.json"
            old = json.loads(pbm.DEFAULT_PATH.read_text(encoding="utf-8"))
            del old["styles"]
            old["formats"]["worlds"]["weight"] = 2.5
            path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            pb = pbm.load(path)
            self.assertIn("highlight", pb["styles"])
            self.assertEqual(pb["formats"]["worlds"]["weight"], 2.5)
            self.assertIn("styles", json.loads(path.read_text(encoding="utf-8")))

    def test_style_is_chosen_and_reweighted(self):
        pb = json.loads(pbm.DEFAULT_PATH.read_text(encoding="utf-8"))
        styles = {pbm.choose(pb, date(2026, 9, 10), rng=random.Random(i)).style for i in range(60)}
        self.assertEqual(styles, set(pb["styles"]))
        new = pbm.reweight(pb, {"styles": {"boxed": 2.0}})
        self.assertGreater(new["styles"]["boxed"]["weight"], 1.0)


SCOUT_RES = {
    "hooks": [
        {"pattern": "Сделал фото, которое *мама* приняла за настоящее", "idea": "реакция близких", "formats": ["before_after", "nope"]},
        {"pattern": "Скинул боту несколько селфи — вот что он сделал", "idea": "дубль", "formats": []},
        {"pattern": "x" * 120, "idea": "слишком длинный"},
    ],
    "trend_notes": ["Первые 0,5 с — сразу результат, без заставок", "Текст по 3–5 слов, крупно"],
    "themes": [{"id": "Old Money!", "title": "Old money", "scenes": ["яхта", "теннис", "особняк", "кабриолет"]},
               {"id": "x", "title": "мусор", "scenes": ["a"]}],
    "summary": "Сдвиг к реакциям и коротким текстам.",
}


class ScoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), gemini_api_key="k")
        self.db = DB(self.cfg.db_path)
        self.now = datetime(2026, 10, 20, 9, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def _ai(self, res=SCOUT_RES, sources=None):
        ai = mock.Mock()
        ai.research.return_value = (res, sources or [{"title": "vc.ru", "uri": "https://vc.ru/x"}])
        return ai

    def test_run_adds_trend_hooks_themes_and_notes(self):
        report = scout.run(self.cfg, self.db, self._ai(), now=self.now)
        pb = pbm.load(self.cfg.playbook_path)
        trend_hooks = {k: v for k, v in pb["hooks"].items() if v.get("source") == "trend"}
        self.assertEqual(len(trend_hooks), 1)  # дубль и слишком длинный отброшены
        h = next(iter(trend_hooks.values()))
        self.assertNotIn("*", h["pattern"])
        self.assertEqual(h["formats"], ["before_after"])
        self.assertIn("old_money", pb["themes"])
        self.assertNotIn("x", pb["themes"])
        self.assertEqual(len(pb["trend_notes"]), 2)
        self.assertIn("vc.ru", report)
        self.assertIn("Сдвиг к реакциям", report)
        self.assertTrue(self.db.get("scout_last"))

    def test_planner_sees_trend_notes(self):
        scout.run(self.cfg, self.db, self._ai(), now=self.now)
        pb = pbm.load(self.cfg.playbook_path)
        captured = {}

        class AIStub:
            def json(self, system, prompt, images=None, temperature=0.9):
                captured["prompt"] = prompt
                raise AIError("down")
        choice = pbm.Choice("worlds", "eras", "eras", False, None, "highlight")
        p = plan(AIStub(), pb, choice, 3)
        self.assertIn("Первые 0,5 с", captured["prompt"])
        self.assertIn("ключевое слово на жёлтой плашке", captured["prompt"])
        self.assertEqual(p.llm_error, "down")

    def test_prune_removes_failed_trends_after_trial_only(self):
        scout.run(self.cfg, self.db, self._ai(), now=self.now)
        pb = pbm.load(self.cfg.playbook_path)
        key = next(k for k, v in pb["hooks"].items() if v.get("source") == "trend")
        pb["hooks"][key]["weight"] = 0.4
        self.assertEqual(scout.prune(pb, self.now + timedelta(days=10)), [])
        self.assertIn(key, scout.prune(pb, self.now + timedelta(days=40)))
        self.assertIn("sent_selfies", pb["hooks"])  # исходные хуки не трогаем

    def test_trend_hooks_are_capped(self):
        pb = json.loads(pbm.DEFAULT_PATH.read_text(encoding="utf-8"))
        for i in range(20):
            pb["hooks"][f"t{i}"] = {"weight": 1 + i / 100, "formats": ["worlds"], "pattern": f"п{i}",
                                    "source": "trend", "added_at": self.now.isoformat()}
        scout.prune(pb, self.now)
        self.assertEqual(sum(1 for v in pb["hooks"].values() if v.get("source") == "trend"),
                         scout.MAX_TREND_HOOKS)
        self.assertNotIn("t0", pb["hooks"])  # самые слабые ушли первыми

    def test_failure_leaves_playbook_untouched(self):
        ai = mock.Mock()
        ai.research.side_effect = AIError("нужен GEMINI_API_KEY")
        before = pbm.load(self.cfg.playbook_path)
        self.assertIn("не удалась", scout.run(self.cfg, self.db, ai, now=self.now))
        self.assertEqual(pbm.load(self.cfg.playbook_path), before)
        self.assertFalse(self.db.get("scout_last"))


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


class AIProviderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(data_dir=Path(tempfile.mkdtemp()), gemini_api_key="g", openai_api_key="")

    def test_research_parses_grounded_answer(self):
        payload = {"candidates": [{
            "content": {"parts": [{"text": '```json\n{"hooks": [], "summary": "ok"}\n```'}]},
            "groundingMetadata": {"groundingChunks": [
                {"web": {"uri": "https://a.ru/1", "title": "a.ru"}},
                {"web": {"uri": "https://a.ru/1", "title": "a.ru"}},
                {"web": {"uri": "https://b.ru/2", "title": "b.ru"}}]}}]}
        with mock.patch("engine.llm.requests.post", return_value=Resp(200, payload)) as post:
            res, sources = AI(self.cfg).research("s", "p")
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["tools"], [{"google_search": {}}])
        self.assertNotIn("responseMimeType", body["generationConfig"])
        self.assertEqual(res["summary"], "ok")
        self.assertEqual([s["title"] for s in sources], ["a.ru", "b.ru"])

    def test_custom_base_urls_for_proxy_services(self):
        self.cfg.gemini_base_url = "https://proxy.example.ru/google"
        self.cfg.openai_base_url = "https://proxy.example.ru/openai/v1"
        self.cfg.openai_api_key = "sk-star-xyz"
        ok = Resp(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
                        "choices": [{"message": {"content": '{"ok": true}'}}]})
        with mock.patch("engine.llm.requests.post", return_value=ok) as post:
            lines = AI(self.cfg).check()
        urls = [c.args[0] for c in post.call_args_list]
        self.assertEqual(urls[0], "https://proxy.example.ru/google/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertEqual(urls[1], "https://proxy.example.ru/openai/v1/chat/completions")
        self.assertEqual(post.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer g")
        self.assertTrue(all(l.startswith("✅") for l in lines))
        self.assertIn("proxy.example.ru", lines[0])

    def test_errors_are_short(self):
        err = Resp(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                   "details": [{"reason": "API_KEY_INVALID"}] * 5}})
        with mock.patch("engine.llm.requests.post", return_value=err):
            line = AI(self.cfg).check()[0]
        self.assertTrue(line.endswith("API key not valid. Please pass a valid API key."))

    def test_quotes_around_keys_are_ignored(self):
        with mock.patch.dict("os.environ", {"GEMINI_API_KEY": ' "AIzaKEY" '}):
            self.assertEqual(Config().gemini_api_key, "AIzaKEY")

    def test_research_requires_gemini(self):
        self.cfg.gemini_api_key = ""
        with self.assertRaises(AIError):
            AI(self.cfg).research("s", "p")

    def test_check_reports_each_provider(self):
        err = Resp(400, {"error": {"message": "API key not valid. Please pass a valid API key."}})
        with mock.patch("engine.llm.requests.post", return_value=err):
            lines = AI(self.cfg).check()
        self.assertIn("API key not valid", lines[0])
        self.assertIn("❌ Gemini", lines[0])
        self.assertEqual(lines[1], "OpenAI: ключ не задан")


if __name__ == "__main__":
    unittest.main()


class ProxyStyleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(data_dir=Path(tempfile.mkdtemp()), gemini_api_key="sk-cvc-x",
                          gemini_base_url="https://proxy.example/", openai_api_key="sk-star-y",
                          openai_base_url="https://ai.example/v1")

    def test_switches_to_openai_format_when_native_route_missing(self):
        calls = []

        def post(url, timeout=None, **kw):
            calls.append((url, kw.get("headers", {})))
            if ":generateContent" in url:
                return Resp(404, {"error": {"message": "Invalid URL (POST /v1beta/models/...)"}})
            return Resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})
        ai = AI(self.cfg)
        with mock.patch("engine.llm.requests.post", side_effect=post):
            self.assertEqual(ai.json("s", "p"), {"ok": True})
            self.assertEqual(ai.json("s", "p"), {"ok": True})
        self.assertEqual(ai.gemini_style, "openai")
        self.assertEqual(calls[1][0], "https://proxy.example/v1/chat/completions")
        self.assertEqual(calls[1][1]["Authorization"], "Bearer sk-cvc-x")
        # второй запрос сразу в формате OpenAI, без повторной пробы формата Google
        self.assertEqual(sum(1 for u, _ in calls if ":generateContent" in u), 1)

    def test_native_proxy_is_remembered_and_gets_both_auth_headers(self):
        ok = {"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]}
        with mock.patch("engine.llm.requests.post", return_value=Resp(200, ok)) as post:
            AI_ = AI(self.cfg)
            AI_.json("s", "p")
        self.assertEqual(AI_.gemini_style, "native")
        url = post.call_args.args[0]
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(url, "https://proxy.example/v1beta/models/gemini-2.5-flash:generateContent")
        self.assertEqual(headers["Authorization"], "Bearer sk-cvc-x")
        self.assertEqual(headers["x-goog-api-key"], "sk-cvc-x")

    def test_invalid_key_is_not_mistaken_for_wrong_format(self):
        err = Resp(400, {"error": {"message": "API key not valid. Please pass a valid API key."}})
        ai = AI(self.cfg)
        self.cfg.openai_api_key = ""
        with mock.patch("engine.llm.requests.post", return_value=err):
            with self.assertRaises(AIError):
                ai.json("s", "p")
        self.assertIsNone(ai.gemini_style)

    def test_openai_base_url_with_or_without_v1(self):
        for base in ("https://ai.example/v1", "https://ai.example", "https://ai.example/v1/"):
            self.cfg.openai_base_url = base
            self.assertEqual(AI(self.cfg)._openai_url("chat/completions"),
                             "https://ai.example/v1/chat/completions")

    def test_html_page_instead_of_api_is_reported_clearly(self):
        class Html:
            status_code, text = 200, "<html>Магазин ключей</html>"

            def json(self):
                raise ValueError("no json")
        self.cfg.openai_api_key = ""
        self.cfg.gemini_api_style = "native"
        with mock.patch("engine.llm.requests.post", return_value=Html()):
            with self.assertRaises(AIError) as ctx:
                AI(self.cfg).json("s", "p")
        self.assertIn("не JSON", str(ctx.exception))

    def test_research_unavailable_in_openai_format(self):
        self.cfg.gemini_api_style = "openai"
        with self.assertRaises(AIError) as ctx:
            AI(self.cfg).research("s", "p")
        self.assertIn("поиск Google", str(ctx.exception))
