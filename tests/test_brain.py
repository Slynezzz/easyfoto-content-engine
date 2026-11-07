import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from engine import brain, niche, postcard
from engine.config import Config
from engine.db import DB
from engine.llm import AIError, ImageResult


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1024, 1536), (40, 90, 120)).save(buf, "JPEG")
    return buf.getvalue()


DRAFT = {
    "segment": "realestate", "insight": "Риэлтор теряет заявки ночью", "objection": "дорого",
    "hooks": [{"mechanic": m, "text": f"Хук {m}"} for m in ("situation", "mistake", "myth", "loss", "list")],
    "best": 1, "headline": "Вы *теряете* заявки ночью", "subline": "Как это закрыть без нового сотрудника",
    "body": "Клиент написал в 23:40. Ответили утром. " * 5, "hashtags": ["#бизнес", "автоматизация", "#ии",
                                                                        "#боты", "#crm", "#лишний"],
    "image": {"mode": "author", "title": "ночной телефон", "where": "стол", "doing": "звонит телефон",
              "around": "ночь", "light": "мягкий"},
}


class FakeAI:
    def __init__(self, critic_fail=False):
        self.calls = []
        self.critic_fail = critic_fail
        self.image_prompts = []

    def json(self, system, prompt, images=None, temperature=0.9, model=None):
        self.calls.append((system, prompt, model))
        if "редактор-критик" in system:
            if self.critic_fail:
                raise AIError("critic down")
            return {"scores": {"хук": 4}, "fixes": ["усилил первую строку"],
                    "headline": "Заявки *уходят* ночью", "subline": "Что с этим делать",
                    "body": "Финальный текст после критика.", "hashtags": ["#бизнес", "#ии"]}
        return json.loads(json.dumps(DRAFT))

    def image(self, prompt, refs):
        self.image_prompts.append((prompt, list(refs)))
        return ImageResult(png(), "image/jpeg", "fake")


class BrainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), niche="expert", copy_model="strong")
        self.cfg.ensure_dirs()
        niche.activate(self.cfg)
        self.db = DB(self.cfg.db_path)

    def tearDown(self):
        niche.activate(Config(niche="neurophoto"))
        self.db.close()
        self._tmp.cleanup()

    def req(self, platform="ig"):
        return {"title": "Боты для риэлторов", "angle": "", "url": "", "platform": platform, "topic_id": None}

    def test_stage_balance(self):
        strat = brain.strategy()
        self.assertEqual(brain.pick_stage([], strat), "reach")
        recent = [{"stage": "reach"}] * 4 + [{"stage": "trust"}] * 4
        self.assertEqual(brain.pick_stage(recent, strat), "lead")

    def test_write_uses_critic_and_platform_limits(self):
        ai = FakeAI()
        post = brain.write(self.cfg, self.db, ai, self.req("ig"))
        self.assertEqual(post["headline"], "Заявки *уходят* ночью")
        self.assertEqual(post["body"], "Финальный текст после критика.")
        self.assertEqual(post["mechanic"], "mistake")
        self.assertEqual(post["stage"], "reach")
        # нет фото «реф» — лицо автора нельзя, только иллюстрация
        self.assertEqual(post["image"]["mode"], "illustration")
        self.assertEqual(ai.calls[0][2], "strong")
        prompt = ai.calls[0][1]
        self.assertIn("ЭТАП ВОРОНКИ: охват", prompt)
        self.assertIn("КАРТА ВОЗРАЖЕНИЙ", prompt)
        self.assertIn("Instagram", prompt)

    def test_critic_failure_keeps_draft_and_trims_hashtags(self):
        post = brain.write(self.cfg, self.db, FakeAI(critic_fail=True), self.req("ig"))
        self.assertEqual(post["headline"], DRAFT["headline"])
        self.assertEqual(len(post["hashtags"]), 5)          # Instagram — не больше 5
        self.assertTrue(all(t.startswith("#") for t in post["hashtags"]))
        tg = brain.write(self.cfg, self.db, FakeAI(critic_fail=True), self.req("tg"))
        self.assertEqual(tg["hashtags"], [])                 # Telegram — без хэштегов

    def test_make_post_creates_4x5_card(self):
        ai = FakeAI()
        pid = brain.make_post(self.cfg, self.db, ai, self.req("tg"))
        post = self.db.post(pid)
        self.assertEqual((post["format"], post["status"], post["target"]), ("post", "rendered", "tg"))
        self.assertIsNone(post["video_path"])
        with Image.open(post["cover_path"]) as im:
            self.assertEqual(im.size, (1080, 1350))
        self.assertTrue(post["cover_path"].endswith(f"{post['media_token']}.jpg"))
        plan = json.loads(post["script_json"])
        self.assertEqual(plan["script"]["hook"], "Заявки *уходят* ночью")
        self.assertEqual(plan["brain"]["stage"], "reach")
        self.assertEqual(post["caption"], "Финальный текст после критика.")
        self.assertIn("4:5", ai.image_prompts[0][0])
        self.assertEqual(ai.image_prompts[0][1], [])

    def test_author_face_when_refs_and_rotation(self):
        ref = self.cfg.data_dir / "me.jpg"
        Image.new("RGB", (800, 1000), (200, 150, 120)).save(ref)
        self.db.add_asset(str(ref), "ref")
        ai = FakeAI()
        pid = brain.make_post(self.cfg, self.db, ai, self.req("tg"))
        self.assertEqual(json.loads(self.db.post(pid)["script_json"])["brain"]["image_mode"], "author")
        self.assertTrue(ai.image_prompts[-1][1])            # ушли фото-образцы
        # следующий пост — лицо уже было недавно, только иллюстрация
        pid2 = brain.make_post(self.cfg, self.db, ai, self.req("tg"))
        self.assertEqual(json.loads(self.db.post(pid2)["script_json"])["brain"]["image_mode"], "illustration")

    def test_winners_from_leads(self):
        pid = brain.make_post(self.cfg, self.db, FakeAI(), self.req("tg"))
        self.db.x("INSERT INTO leads(user_id, post_id, form_at, created_at, updated_at) VALUES (1, ?, 'x', 'x', 'x')",
                  (pid,))
        self.assertIn("заявок 1", brain.lead_winners(self.db)[0])


class PostcardTests(unittest.TestCase):
    def test_tokens_accent_and_glue(self):
        toks = postcard._tokens("Клиент написал в 23:40. Вы *ответили* в 9:15")
        self.assertIn(("в 23:40.", False), toks)
        self.assertIn(("ответили", True), toks)
        self.assertIn(("в 9:15", False), toks)


class PublishPostTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), niche="expert", ig_user_id="1784",
                          ig_access_token="SECRET", public_base_url="https://ex.app", tg_channel_id="@ch")
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        img = self.cfg.media_dir / "tok_tok_tok_tok_tok1.jpg"
        Image.new("RGB", (1080, 1350)).save(img)
        self.pid = self.db.create_post(status="approved", format="post", hook_id="post_x", theme="stage_reach",
                                       script_json={"script": {"hook": "Хук"}}, caption="Текст поста",
                                       cover_path=str(img), media_token="tok_tok_tok_tok_tok1")

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def test_instagram_image_container(self):
        from engine.instagram import Instagram
        from tests.test_instagram import FakeGraph
        g = FakeGraph(statuses=["FINISHED"])
        with mock.patch.multiple("engine.instagram.requests", get=g.get, post=g.post):
            Instagram(self.cfg, self.db, sleep=lambda s: None).publish(self.pid)
        create = next(c for c in g.calls if c[1].endswith("/media"))[2]
        self.assertEqual(create["image_url"], "https://ex.app/media/tok_tok_tok_tok_tok1.jpg")
        self.assertNotIn("media_type", create)
        self.assertEqual(self.db.post(self.pid)["status"], "published")

    def test_image_insights_do_not_poison_reel_metrics(self):
        from engine.instagram import Instagram
        from tests.test_instagram import FakeGraph
        g = FakeGraph(fail_metric="shares")
        with mock.patch.multiple("engine.instagram.requests", get=g.get, post=g.post):
            m = Instagram(self.cfg, self.db).insights("M1", image=True)
        self.assertEqual(m["plays"], 7)
        self.assertFalse(self.db.get("ig_bad_metrics"))

    def test_tg_channel_sends_photo(self):
        from engine.tgchannel import TGChannel
        api = mock.Mock()
        api.send_photo.return_value = {"message_id": 5}
        TGChannel(self.cfg, self.db, api).publish(self.pid)
        api.send_photo.assert_called_once()
        api.send_video.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class AdminCreatePostTests(unittest.TestCase):
    def setUp(self):
        from engine.admin_bot import AdminBot, Services
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self._tmp.name), niche="expert", admin_chat_id=111)
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.tid = self.db.add_topic("news", "Telegram выпустил ботов-агентов", "что это бизнесу", "")
        img = self.cfg.media_dir / "tok_tok_tok_tok_tok2.jpg"
        Image.new("RGB", (1080, 1350)).save(img)
        self.calls = []

        def make_request(topic_id, platform, kind="reel"):
            self.calls.append((topic_id, platform, kind))
            return self.db.create_post(status="rendered", format="post", hook_id="post_x", theme="stage_trust",
                                       target=platform, topic_id=topic_id, cover_path=str(img),
                                       media_token="tok_tok_tok_tok_tok2", caption="Текст",
                                       script_json=json.dumps({"script": {"hook": "Хук *поста*"}, "llm_ok": True,
                                                               "brain": {"stage": "trust", "mechanic": "myth",
                                                                         "objection": "сложно"}}))
        self.api = mock.Mock()
        self.api.send_message.return_value = {"message_id": 1}
        self.api.send_photo.return_value = {"message_id": 2}
        self.bot = AdminBot(self.cfg, self.db, self.api, Services(
            make_one=lambda: 0, instagram_ready=lambda: False, run_analysis=lambda: "", stats_text=lambda: "",
            make_request=make_request))

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def cb(self, data):
        self.bot.on_callback({"id": "c", "data": data, "message": {"message_id": 9}})

    def test_platform_then_kind_then_photo_approval(self):
        self.cb(f"cp:{self.tid}:tg")
        args = self.api.send_message.call_args.args
        kb = args[2] if len(args) > 2 else self.api.send_message.call_args.kwargs.get("reply_markup")
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], f"ck:{self.tid}:tg:post")
        self.assertEqual(self.calls, [])
        self.cb(f"ck:{self.tid}:tg:post")
        self.assertEqual(self.calls, [(self.tid, "tg", "post")])
        self.api.send_photo.assert_called_once()
        caption = self.api.send_photo.call_args.args[2]
        self.assertIn("этап: доверие", caption)
        self.assertIn("Снимает возражение: сложно", caption)

    def test_youtube_skips_kind_question(self):
        self.cb(f"cp:{self.tid}:yt")
        self.assertEqual(self.calls, [(self.tid, "yt", "reel")])
