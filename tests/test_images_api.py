import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from engine import images
from engine.config import Config
from engine.db import DB
from engine.llm import AI, AIError, AIRefused, extract_image, rank_image_models
from tests.fixtures import make


def png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (64, 112), (200, 100, 50)).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


B64 = png_b64()


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)
        self.content = b""
        self.headers = {}

    def json(self):
        return self._data


class ExtractTests(unittest.TestCase):
    def test_all_known_response_shapes(self):
        shapes = {
            "images api": {"data": [{"b64_json": B64}]},
            "openrouter": {"choices": [{"message": {"content": "", "images": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}]}}]},
            "content parts": {"choices": [{"message": {"content": [
                {"type": "text", "text": "вот"}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}}]}}]},
            "markdown": {"choices": [{"message": {"content": f"Готово ![img](data:image/png;base64,{B64})"}}]},
            "inline_data": {"choices": [{"message": {"content": [{"inline_data": {"mime_type": "image/png", "data": B64}}]}}]},
        }
        for name, data in shapes.items():
            with self.subTest(shape=name):
                got = extract_image(data)
                self.assertIsNotNone(got)
                self.assertEqual(Image.open(io.BytesIO(got[0])).size, (64, 112))

    def test_http_url_is_downloaded(self):
        dl = mock.Mock(status_code=200, content=base64.b64decode(B64), headers={"Content-Type": "image/png"})
        with mock.patch("engine.llm.requests.get", return_value=dl) as get:
            got = extract_image({"choices": [{"message": {"content": "Картинка: https://cdn.example/x/1.png"}}]})
        get.assert_called_once()
        self.assertEqual(got[1], "image/png")

    def test_text_only_answer(self):
        self.assertIsNone(extract_image({"choices": [{"message": {"content": "Не могу создать это изображение"}}]}))

    def test_rank_image_models(self):
        ids = ["gpt-5.4-mini", "gemini-3.8-flash", "gpt-image-1.5", "gemini-3.1-flash-image",
               "gemini-2.5-flash-image", "dall-e-3", "imagen-4"]
        self.assertEqual(rank_image_models(ids)[:3], ["gemini-3.1-flash-image", "gemini-2.5-flash-image", "gpt-image-1.5"])
        self.assertNotIn("gemini-3.8-flash", rank_image_models(ids))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.refs = [make(self.tmp / f"r{i}.jpg", (600, 900), i) for i in range(2)]
        self.cfg = Config(data_dir=self.tmp, gemini_api_key="sk-cvc", gemini_base_url="https://saint.example",
                          gemini_api_style="openai", openai_api_key="sk-star",
                          openai_base_url="https://ai.star.example/v1", image_api_key="", image_model="")
        self.sleep = mock.patch("engine.llm.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_gemini_chat_route_with_auto_model_pick(self):
        calls = []

        def post(url, timeout=None, json=None, **kw):
            calls.append((url, json.get("model") if json else kw.get("data", {}).get("model")))
            if json and json["model"] == "gemini-2.5-flash-image":
                return Resp(400, {"error": {"message": "Unknown model"}})
            return Resp(200, {"choices": [{"message": {"content": f"![x](data:image/png;base64,{B64})"}}]})

        def get(url, headers=None, timeout=None):
            return Resp(200, {"data": [{"id": "gemini-3.8-flash"}, {"id": "gemini-3.1-flash-image"}]})
        ai = AI(self.cfg)
        ai.gemini_style = "openai"
        with mock.patch("engine.llm.requests.post", side_effect=post), \
             mock.patch("engine.llm.requests.get", side_effect=get):
            res = ai.image("сцена", self.refs)
        self.assertIn("Gemini/chat/gemini-3.1-flash-image", res.provider)
        body_url = calls[-1][0]
        self.assertEqual(body_url, "https://saint.example/v1/chat/completions")
        # второй раз сразу по сработавшему пути
        self.assertEqual(ai.image_routes()[0][:2], ("Gemini", "chat"))

    def test_falls_through_to_openai_edits(self):
        def post(url, timeout=None, json=None, data=None, files=None, **kw):
            if "saint.example" in url:
                return Resp(200, {"choices": [{"message": {"content": "Извините, не могу"}}]})
            self.assertEqual(url, "https://ai.star.example/v1/images/edits")
            self.assertEqual(data["model"], "gpt-image-1.5")
            self.assertEqual(len(files), 2)
            return Resp(200, {"data": [{"b64_json": B64}]})
        ai = AI(self.cfg)
        ai.gemini_style = "openai"
        with mock.patch("engine.llm.requests.post", side_effect=post):
            res = ai.image("сцена", self.refs)
        self.assertTrue(res.provider.startswith("OpenAI/edits"))

    def test_dedicated_image_key_goes_first(self):
        self.cfg.image_api_key, self.cfg.image_api_base_url, self.cfg.image_model = "K", "https://img.example/v1", "gpt-image-1.5"
        ai = AI(self.cfg)
        ai.gemini_style = "openai"
        routes = ai.image_routes()
        self.assertEqual(routes[0][:3], ("IMAGE_API", "edits", "https://img.example"))

    def test_all_refusals_raise_refused(self):
        ai = AI(self.cfg)
        ai.gemini_style = "openai"
        with mock.patch("engine.llm.requests.post",
                        return_value=Resp(200, {"choices": [{"message": {"content": "нет"}}], "data": []})):
            with self.assertRaises(AIRefused):
                ai.image("сцена", self.refs)


class PickRefsTests(unittest.TestCase):
    def test_before_photos_serve_as_refs_and_test_generate(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = Config(data_dir=tmp)
        cfg.ensure_dirs()
        db = DB(cfg.db_path)
        make(cfg.assets_dir / "before" / "b.jpg", (600, 900), 1)
        images.scan(cfg, db)
        self.assertEqual(len(images.pick_refs(db)), 1)
        ai = mock.Mock()
        ai.image.return_value = mock.Mock(data=base64.b64decode(B64), provider="Gemini/chat/m")
        path, route, secs = images.test_generate(cfg, db, ai)
        self.assertTrue(path.exists())
        self.assertEqual(route, "Gemini/chat/m")
        self.assertEqual(db.assets("generated"), [])  # пробный кадр не попадает в библиотеку
        db.close()

    def test_no_face_photos_explains(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = Config(data_dir=tmp)
        db = DB(cfg.db_path)
        with self.assertRaises(AIError) as ctx:
            images.test_generate(cfg, db, mock.Mock())
        self.assertIn("«реф»", str(ctx.exception))
        db.close()


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.refs = [make(self.tmp / "r.jpg", (600, 900), 1)]
        self.cfg = Config(data_dir=self.tmp, gemini_api_key="g", gemini_base_url="https://saint.example",
                          gemini_api_style="openai", openai_api_key="", image_api_key="")
        self.sleep = mock.patch("engine.llm.time.sleep")
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_trailing_text_after_base64_is_not_a_crash(self):
        data = {"choices": [{"message": {"content": f"![x](data:image/png;base64,{B64})\n\nHere is your image"}}]}
        self.assertIsNotNone(extract_image(data))
        broken = {"choices": [{"message": {"content": "data:image/png;base64,AAAA=AAA"}}]}
        self.assertIsNone(extract_image(broken))

    def test_link_to_non_image_is_rejected(self):
        page = mock.Mock(status_code=200, content=b"<html>not an image</html>", headers={"Content-Type": "text/html"})
        with mock.patch("engine.llm.requests.get", return_value=page):
            self.assertIsNone(extract_image({"choices": [{"message": {"content": "https://x.example/a.png"}}]}))

    def test_text_answer_is_not_retried_with_modalities(self):
        calls = []

        def post(url, timeout=None, json=None, **kw):
            calls.append(json)
            return Resp(200, {"choices": [{"message": {"content": "Не могу нарисовать"}}]})
        with mock.patch("engine.llm.requests.post", side_effect=post):
            with self.assertRaises(AIRefused):
                AI(self.cfg).image("сцена", self.refs)
        self.assertEqual(len(calls), 1)

    def test_empty_answer_retries_with_modalities_and_remembers(self):
        calls = []

        def post(url, timeout=None, json=None, **kw):
            calls.append("modalities" in json)
            if "modalities" in json:
                return Resp(200, {"choices": [{"message": {"content": "", "images": [
                    {"image_url": {"url": f"data:image/png;base64,{B64}"}}]}}]})
            return Resp(200, {"choices": [{"message": {"content": ""}}]})
        ai = AI(self.cfg)
        with mock.patch("engine.llm.requests.post", side_effect=post):
            ai.image("сцена", self.refs)
            ai.image("сцена", self.refs)
        self.assertEqual(calls, [False, True, True])

    def test_timeout_is_not_retried(self):
        import requests as rq
        with mock.patch("engine.llm.requests.post", side_effect=rq.Timeout("slow")) as post:
            with self.assertRaises(AIError) as ctx:
                AI(self.cfg).image("сцена", self.refs)
        self.assertEqual(post.call_count, 1)
        self.assertIn("не платить дважды", str(ctx.exception))

    def test_working_model_is_reused(self):
        ai = AI(self.cfg)
        ai.image_route = ("Gemini", "chat", "https://saint.example", "gemini-3.1-flash-image")
        self.assertEqual(ai.image_routes()[0][4], "gemini-3.1-flash-image")

    def test_daily_image_budget(self):
        cfg = Config(data_dir=self.tmp / "d", max_images_per_day=2)
        cfg.ensure_dirs()
        db = DB(cfg.db_path)
        make(cfg.assets_dir / "refs" / "a.jpg", (600, 900), 1)
        images.scan(cfg, db)
        ai = mock.Mock()
        ai.image.return_value = mock.Mock(data=base64.b64decode(B64), provider="x")
        images.generate(cfg, db, ai, {"title": "a"}, "cinema")
        images.test_generate(cfg, db, ai)
        with self.assertRaises(AIError) as ctx:
            images.generate(cfg, db, ai, {"title": "b"}, "cinema")
        self.assertIn("дневной лимит", str(ctx.exception))
        self.assertEqual(ai.image.call_count, 2)
        self.assertEqual(images.images_used_today(db), 2)
        db.close()
