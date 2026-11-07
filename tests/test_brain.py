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
        d = json.loads(json.dumps(DRAFT))
        if "ПОСТ С ПЕРЕПИСКОЙ" in prompt:
            d["dialog"] = {"bot_name": "Агентство · помощник", "time": "23:40",
                           "messages": [{"from": "client", "text": "Квартира ещё продаётся?"},
                                        {"from": "bot", "text": "Да! Показать похожие?"},
                                        {"from": "client", "text": "Да"}, {"from": "bot", "text": "Вот три варианта."}]}
        if "КАРУСЕЛЬ" in prompt:
            d["slides"] = [{"title": f"Слайд *{i}*", "text": "MAX — 50 млн пользователей по данным VK",
                            "source": "VK" if i == 1 else ""} for i in range(1, 5)]
        return d

    def image(self, prompt, refs):
        self.image_prompts.append((prompt, list(refs)))
        return ImageResult(png(), "image/jpeg", "fake")

    no_research = False

    def research(self, system, prompt, temperature=0.4):
        if self.no_research:
            raise AIError("поиск недоступен")
        return ({"answer": "MAX растёт и предустановлен на смартфоны",
                 "facts": [{"fact": "MAX — 50 млн пользователей", "source": "VK", "url": "https://vk.com/press",
                            "date": "2026"},
                           {"fact": "внешняя ссылка", "source": "Блог", "url": "https://evil.example", "date": ""}]},
                [{"uri": "https://vk.com/press"}])


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
        self.assertIn("MAX — 50 млн пользователей", prompt := ai.calls[-2][1])
        self.assertIn("ТРЕБОВАНИЯ К ТЕКСТУ", prompt)
        self.assertIn("MAX — 50 млн пользователей", ai.calls[-1][1])      # критик сверяет цифры
        self.assertEqual([f["url"] for f in post["research"]["facts"]], ["https://vk.com/press", ""])
        self.assertEqual(post["mechanic"], "mistake")
        self.assertEqual(post["stage"], "reach")
        # нет фото «реф» — лицо автора нельзя, только иллюстрация
        self.assertEqual(post["image"]["mode"], "illustration")
        self.assertEqual(ai.calls[-2][2], "strong")
        prompt = ai.calls[-2][1]
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
        self.assertEqual(post["caption"], "Финальный текст после критика.\n\nИсточники:\n— VK: https://vk.com/press")
        self.assertEqual(plan["brain"]["research"]["method"], "Gemini с поиском Google")
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


class CarouselTests(BrainTests):
    def test_carousel_renders_slides_and_cta(self):
        pid = brain.make_post(self.cfg, self.db, FakeAI(), self.req("tg"), kind="carousel")
        post = self.db.post(pid)
        self.assertEqual(post["format"], "carousel")
        slides = json.loads(post["script_json"])["slides"]
        self.assertEqual(len(slides), 5)                       # 4 слайда + призыв
        self.assertTrue(all(Path(x).exists() and Path(x).stem.startswith(post["media_token"] + "_") for x in slides))
        with Image.open(slides[-1]) as im:
            self.assertEqual(im.size, (1080, 1350))

    def test_carousel_publishing(self):
        from engine.instagram import Instagram
        from engine.tgchannel import TGChannel
        from tests.test_instagram import FakeGraph
        cfg = self.cfg
        cfg.ig_user_id, cfg.ig_access_token, cfg.public_base_url, cfg.tg_channel_id = "1784", "S", "https://ex.app", "@c"
        pid = brain.make_post(cfg, self.db, FakeAI(), self.req("ig"), kind="carousel")
        self.db.update_post(pid, status="approved")
        g = FakeGraph(statuses=["FINISHED"])
        with mock.patch.multiple("engine.instagram.requests", get=g.get, post=g.post):
            Instagram(cfg, self.db, sleep=lambda s: None).publish(pid)
        posts = [c[2] for c in g.calls if c[0] == "POST" and c[1].endswith("/media")]
        self.assertEqual(sum(1 for c in posts if c.get("is_carousel_item")), 6)   # обложка + 5 слайдов
        self.assertEqual(posts[-1]["media_type"], "CAROUSEL")
        api = mock.Mock()
        api.send_media_group.return_value = [{"message_id": 7}]
        TGChannel(cfg, self.db, api).publish(pid)
        self.assertEqual(len(api.send_media_group.call_args.args[1]), 6)


class DialogAndHotTests(BrainTests):
    def test_dialog_post_without_image_model(self):
        ai = FakeAI()
        pid = brain.make_post(self.cfg, self.db, ai, self.req("vk"), kind="dialog")
        post = self.db.post(pid)
        self.assertEqual(post["format"], "dialog")
        self.assertEqual(ai.image_prompts, [])                       # картинка — макет, без генерации
        with Image.open(post["cover_path"]) as im:
            self.assertEqual(im.size, (1080, 1350))
        self.assertEqual(json.loads(post["script_json"])["brain"]["dialog"]["messages"][0]["from"], "client")

    def test_headline_cleanup(self):
        self.assertEqual(brain.clean_headline("«Нам рано *автоматизироваться*» — а клиент ушёл"),
                         "Нам рано *автоматизироваться*, а клиент ушёл")
        self.assertEqual(brain.clean_subline("Разбираю возражение: почему начинать с одного процесса"),
                         "Почему начинать с одного процесса")
        toks = postcard._tokens("клиент уже *ушёл*.")
        self.assertEqual(toks[-2:], [("ушёл" + postcard.WJ, True), (".", False)])

    def test_hot_topics_alert(self):
        from engine import scout
        from engine.app import Engine
        scout.save_topics(self.db, {"topics": [{"kind": "news", "title": "MAX открыл ботов для бизнеса",
                                                "angle": "что это значит", "source": "https://x.ru"},
                                               {"kind": "trend", "title": "Карусели растут"}]})
        eng = Engine.__new__(Engine)
        eng.cfg, eng.db = self.cfg, self.db
        said = []
        eng.say = lambda text, markup=None: said.append((text, markup))
        self.assertTrue(eng.hot_alert())
        self.assertIn("MAX открыл ботов", said[0][0])
        self.assertTrue(said[0][1]["inline_keyboard"][0][0]["callback_data"].startswith("ct:"))
        self.assertFalse(eng.hot_alert())                             # второй раз — уже сообщили


class ResearchFallbackTests(unittest.TestCase):
    def test_no_search_means_no_numbers(self):
        from engine import research
        ai = FakeAI()
        ai.no_research = True
        res = research.run(Config(data_dir=Path(tempfile.mkdtemp())), ai, {"title": "t"})
        self.assertEqual(res["facts"], [])
        self.assertIn("поиск недоступен", res["error"])
        self.assertIn("Не используй никаких цифр", research.facts_block(res))

    def test_instagram_caption_has_no_links(self):
        post = {"body": "Текст", "hashtags": ["#a"], "sources": [{"source": "VK", "url": "https://vk.com/p"}]}
        self.assertEqual(brain.compose_caption(post, "ig"), "Текст\n\n#a")
        self.assertIn("https://vk.com/p", brain.compose_caption(post, "vk"))


class PostcardTests(unittest.TestCase):
    def test_tokens_accent_and_glue(self):
        toks = postcard._tokens("Клиент написал в 23:40. Вы *ответили* в 9:15")
        self.assertIn(("ответили", True), toks)
        self.assertEqual(toks[-2:], [("в" + postcard.NBSP, False), ("9:15", False)])
        from PIL import ImageFont
        from engine.video.text import FONT_BOLD
        font = ImageFont.truetype(str(FONT_BOLD), 80)
        for line in postcard._wrap(postcard._tokens("Почему MAX уже стоит на *каждом* смартфоне"), font, 600):
            self.assertFalse(line[-1][0].endswith(postcard.NBSP))  # предлог не висит в конце строки


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


class SafetyAndTopicsTests(unittest.TestCase):
    def test_copy_of_reference_is_detected(self):
        tmp = Path(tempfile.mkdtemp())
        ref = tmp / "me.jpg"
        src = Image.new("RGB", (800, 1000), (200, 150, 120))
        from PIL import ImageDraw
        ImageDraw.Draw(src).ellipse((200, 200, 600, 700), fill=(40, 40, 40))
        src.save(ref)
        same = Image.open(ref).convert("RGB").resize((1024, 1536))
        self.assertTrue(brain.looks_like_copy(same, [str(ref)]))
        other = Image.new("RGB", (1024, 1536), (20, 60, 160))
        self.assertFalse(brain.looks_like_copy(other, [str(ref)]))

    def test_edits_respects_reference_limit(self):
        from engine import llm
        tmp = Path(tempfile.mkdtemp())
        refs = []
        for i in range(6):
            p = tmp / f"r{i}.jpg"
            Image.new("RGB", (10, 10)).save(p)
            refs.append(str(p))
        sent = []

        def fake_post(url, files=None, **kw):
            sent.append(len(files or []))
            if len(sent) == 1 and len(files) > 3:
                raise AIError("HTTP 400: at most 3 input images are supported")
            return {"data": [{"b64_json": __import__("base64").b64encode(png()).decode()}]}

        with mock.patch.object(llm, "_post", fake_post):
            res = llm.AI._edits_image("https://x", "k", "m", "p", refs)
        self.assertEqual(sent, [4, 3])          # не больше 4, а при отказе — сколько разрешила модель
        self.assertEqual(res.provider, "edits")

    def test_create_without_topics_starts_search(self):
        from engine.admin_bot import AdminBot, Services
        tmp = Path(tempfile.mkdtemp())
        cfg = Config(data_dir=tmp, niche="expert", admin_chat_id=1)
        cfg.ensure_dirs()
        db = DB(cfg.db_path)
        api = mock.Mock()
        api.send_message.return_value = {"message_id": 1}
        calls = []

        def scout():
            calls.append(1)
            db.add_topic("news", "MAX открыл ботов для бизнеса", "", "")
            return "Тренды обновлены"
        bot = AdminBot(cfg, db, api, Services(make_one=lambda: 0, instagram_ready=lambda: False,
                                              run_analysis=lambda: "", stats_text=lambda: "", scout_trends=scout))
        bot.run_bg = lambda fn: fn()
        bot.on_command("/create", [])
        self.assertEqual(calls, [1])
        last = api.send_message.call_args.args
        self.assertIn("MAX открыл ботов", json.dumps(last[2], ensure_ascii=False))
        bot.on_command("/create", [])           # темы уже есть — второй поиск не нужен
        self.assertEqual(calls, [1])
