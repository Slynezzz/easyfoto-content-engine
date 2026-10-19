import json
import random
import tempfile
import unittest
from collections import Counter
from datetime import date
from pathlib import Path

from engine import playbook as pbm
from engine.llm import AIError, parse_json
from engine.planner import plan


class FakeAI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def json(self, system, prompt, images=None, temperature=0.9):
        self.prompts.append((system, prompt))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


GOOD = {
    "hook": "Скинул боту пять селфи — и вот я в кино",
    "captions": ["Нуар", "Италия 60-х", "Неон", "Театр очень-очень длинная подпись сцены"],
    "cta_title": "Хочешь так же?",
    "cta_sub": "Бот — по ссылке в профиле",
    "post_caption": "Ни одного фотографа, ни одной студии. " * 4,
    "hashtags": ["нейрофото", "#aiphoto", "#фото сессия"],
}


class PlaybookTests(unittest.TestCase):
    def setUp(self):
        self.pb = json.loads(pbm.DEFAULT_PATH.read_text(encoding="utf-8"))

    def test_load_creates_working_copy(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "pb.json"
            pb = pbm.load(path)
            self.assertTrue(path.exists())
            self.assertIn("before_after", pb["formats"])

    def test_seasonal_themes_only_in_season(self):
        summer = pbm.theme_weights(self.pb, date(2026, 7, 10))
        self.assertEqual(summer["newyear"], 0)
        self.assertEqual(summer["halloween"], 0)
        dec = pbm.theme_weights(self.pb, date(2026, 12, 20))
        jan = pbm.theme_weights(self.pb, date(2027, 1, 5))
        self.assertGreater(dec["newyear"], 1.0)
        self.assertGreater(jan["newyear"], 1.0)  # окно через Новый год
        self.assertEqual(dec["halloween"], 0)

    def test_hook_matches_format_and_season_hook_hidden_off_season(self):
        rng = random.Random(1)
        for _ in range(300):
            c = pbm.choose(self.pb, date(2026, 9, 10), rng=rng)  # вне всех сезонов
            self.assertIn(c.format, self.pb["hooks"][c.hook_id]["formats"])
            self.assertNotEqual(c.hook_id, "season")
            self.assertIsNone(c.occasion)

    def test_new_year_dominates_in_december(self):
        rng = random.Random(2)
        themes = Counter(pbm.choose(self.pb, date(2026, 12, 1), explore_share=0, rng=rng).theme
                         for _ in range(500))
        self.assertEqual(themes.most_common(1)[0][0], "newyear")

    def test_reweight_moves_toward_scores_and_keeps_floor(self):
        new = pbm.reweight(self.pb, {"formats": {"before_after": 3.0, "worlds": 0.0}}, note="t")
        self.assertGreater(new["formats"]["before_after"]["weight"], 1.0)
        self.assertLess(new["formats"]["worlds"]["weight"], 1.0)
        self.assertGreaterEqual(new["formats"]["worlds"]["weight"], pbm.MIN_WEIGHT)
        self.assertEqual(new["version"], self.pb["version"] + 1)
        self.assertEqual(self.pb["formats"]["before_after"]["weight"], 1.0)  # оригинал не тронут


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.pb = json.loads(pbm.DEFAULT_PATH.read_text(encoding="utf-8"))
        self.choice = pbm.Choice("before_after", "sent_selfies", "cinema", False, None)

    def test_valid_response_is_clipped_and_normalized(self):
        ai = FakeAI([GOOD])
        p = plan(ai, self.pb, self.choice, 4, photo_notes=["a", "b", "c", "d"])
        self.assertTrue(p.llm_ok)
        self.assertEqual(len(p.script["captions"]), 4)
        self.assertTrue(all(len(c) <= 24 for c in p.script["captions"]))
        self.assertEqual(p.hashtags[0], "#нейрофото")
        self.assertIn("#фотосессия", p.hashtags)
        self.assertIn("Правила", ai.prompts[0][0])
        self.assertIn("1. a", ai.prompts[0][1])

    def test_falls_back_to_template_when_ai_down(self):
        ai = FakeAI([AIError("down"), AIError("down")])
        p = plan(ai, self.pb, self.choice, 3, photo_notes=["Нуар", "Неон", "Театр"])
        self.assertFalse(p.llm_ok)
        self.assertEqual(p.script["captions"], ["Нуар", "Неон", "Театр"])
        self.assertIn("ссылке в профиле", p.post_caption)

    def test_retries_once_on_bad_json_shape(self):
        ai = FakeAI([{"hook": "x"}, GOOD])  # первый ответ без текста поста
        p = plan(ai, self.pb, self.choice, 4)
        self.assertTrue(p.llm_ok)
        self.assertEqual(len(ai.prompts), 2)

    def test_generate_mode_requires_scenes(self):
        scene = {"title": "Нуар", "where": "улица", "wearing": "плащ", "doing": "курит",
                 "around": "дождь", "light": "контровой"}
        ok = dict(GOOD, scenes=[scene] * 4)
        p = plan(FakeAI([ok]), self.pb, self.choice, 4, generate_scenes=True)
        self.assertEqual(len(p.scenes), 4)
        self.assertEqual(p.script["captions"][0], "Нуар")
        with self.assertRaises(AIError):
            plan(FakeAI([GOOD, GOOD]), self.pb, self.choice, 4, generate_scenes=True)

    def test_parse_json_handles_code_fences(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('Вот ответ: {"a": [1, 2]} готово'), {"a": [1, 2]})


if __name__ == "__main__":
    unittest.main()
