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
