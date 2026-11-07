import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine import analyst, funnel, leads
from engine.config import Config
from engine.db import DB

OWNER = 111
USER = 555


class FakeAPI:
    def __init__(self):
        self.sent = []
        self._mid = 1000
        self.fail_owner = False

    def _m(self):
        self._mid += 1
        return {"message_id": self._mid}

    def call(self, method, **kw):
        self.sent.append((method, kw))
        return {}

    def send_message(self, chat_id, text, reply_markup=None, reply_to=None):
        if self.fail_owner and chat_id == OWNER:
            from engine.telegram import TelegramError
            raise TelegramError("sendMessage: Forbidden: bot can't initiate conversation")
        self.sent.append(("message", {"chat_id": chat_id, "text": text, "markup": reply_markup,
                                      "reply_to": reply_to}))
        return self._m()

    def send_document(self, chat_id, path, caption="", reply_markup=None):
        self.sent.append(("document", {"chat_id": chat_id, "path": str(path), "caption": caption}))
        return self._m()

    def copy_message(self, chat_id, from_chat_id, message_id, reply_markup=None):
        self.sent.append(("copy", {"chat_id": chat_id, "from": from_chat_id, "message_id": message_id}))
        return self._m()

    def answer_callback(self, cid, text=""):
        self.sent.append(("answer", {"text": text}))

    def edit_markup(self, chat_id, message_id, reply_markup):
        self.sent.append(("markup", {"label": reply_markup["inline_keyboard"][0][0]["text"]}))

    def to(self, chat_id):
        return [k for m, k in self.sent if m == "message" and k["chat_id"] == chat_id]


def user_msg(text=None, uid=USER, mid=1, **extra):
    m = {"message_id": mid, "chat": {"id": uid, "type": "private"},
         "from": {"id": uid, "first_name": "Анна", "username": "anna_salon"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return {"message": m}


def cb(data, uid=USER, mid=1):
    return {"callback_query": {"id": "c", "data": data, "from": {"id": uid, "first_name": "Анна"},
                               "message": {"message_id": mid, "chat": {"id": uid}}}}


class LeadBotTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.cfg = Config(data_dir=tmp / "data", admin_chat_id=OWNER, niche="expert", lead_bot_token="x",
                          bot_username="slyness_bot", public_base_url="https://ex.app", tg_channel_id="@proj_reality")
        self.cfg.ensure_dirs()
        self.db = DB(self.cfg.db_path)
        self.api = FakeAPI()
        self.said = []
        self.bot = leads.LeadBot(self.cfg, self.db, self.api, fallback_say=self.said.append)

    def run_form(self):
        h = self.bot.handle
        h(user_msg("/start utm_vk_clip_p12"))
        h(cb("lb:form"))
        h(cb("lb:sph:0"))
        h(user_msg("Заявки с Авито теряются ночью", mid=2))
        h(cb("lb:tried0"))
        h(cb("lb:ctg"))

    def test_start_records_source_and_greets_with_declension(self):
        self.bot.handle(user_msg("/start utm_vk_clip_p12"))
        row = leads.get(self.db, USER)
        self.assertEqual((row["source"], row["medium"], row["post_id"]), ("vk", "clip", 12))
        text = self.api.to(USER)[-1]["text"]
        self.assertIn("бот Максима Карпова", text)
        self.assertIn("Максим лично", text)
        # повторный приход по другой ссылке не перезаписывает первый источник
        self.bot.handle(user_msg("/start utm_ig_bio"))
        self.assertEqual(leads.get(self.db, USER)["source"], "vk")

    def test_magnet_sends_bundled_pdf_and_offers_review(self):
        self.bot.handle(user_msg("/start"))
        self.bot.handle(cb("lb:magnet"))
        doc = [k for m, k in self.api.sent if m == "document"][0]
        self.assertTrue(Path(doc["path"]).is_file())
        self.assertTrue(doc["path"].endswith(".pdf"))
        row = leads.get(self.db, USER)
        self.assertEqual(row["status"], "magnet")
        self.assertIsNotNone(row["magnet_at"])
        self.assertEqual(self.api.to(USER)[-1]["markup"]["inline_keyboard"][0][0]["callback_data"], "lb:form")

    def test_full_form_sends_card_to_owner_with_source(self):
        self.run_form()
        row = leads.get(self.db, USER)
        self.assertEqual(row["status"], "lead")
        self.assertEqual(row["sphere"], "Недвижимость")
        self.assertEqual(row["tried"], "пока ничего")
        card = self.api.to(OWNER)[-1]
        self.assertIn("Заявка на бесплатный разбор", card["text"])
        self.assertIn("Заявки с Авито теряются ночью", card["text"])
        self.assertIn("VK, ролик #12", card["text"])
        self.assertIn("@anna_salon", card["text"])
        consent = [m["text"] for m in self.api.to(USER) if "соглашаетесь" in m["text"]]
        self.assertTrue(consent and "https://ex.app/leads-privacy" in consent[0])
        self.assertIn("t.me/proj_reality", self.api.to(USER)[-1]["text"])

    def test_owner_reply_is_relayed_and_marks_contacted(self):
        self.run_form()
        card_id = self.db.one("SELECT MIN(owner_msg_id) m FROM lead_msgs")["m"]
        self.bot.handle({"message": {"message_id": 77, "chat": {"id": OWNER, "type": "private"},
                                     "from": {"id": OWNER}, "text": "Здравствуйте! Давайте разберём",
                                     "reply_to_message": {"message_id": card_id}}})
        copy = [k for m, k in self.api.sent if m == "copy"][-1]
        self.assertEqual((copy["chat_id"], copy["from"], copy["message_id"]), (USER, OWNER, 77))
        self.assertEqual(leads.get(self.db, USER)["status"], "contacted")

    def test_voice_answer_forwarded_with_card(self):
        h = self.bot.handle
        h(user_msg("/start"))
        h(cb("lb:form"))
        h(user_msg("Салон", mid=2))
        h(user_msg(None, mid=3, voice={"file_id": "v"}))
        h(cb("lb:tried0"))
        h(cb("lb:ctg"))
        copies = [k for m, k in self.api.sent if m == "copy"]
        self.assertEqual(copies[-1]["message_id"], 3)
        self.assertIsNone(leads.get(self.db, USER)["extra_msgs"])

    def test_phone_contact(self):
        h = self.bot.handle
        h(user_msg("/start"))
        h(cb("lb:form"))
        h(cb("lb:sph:1"))
        h(user_msg("бронь столиков", mid=2))
        h(user_msg("ничего", mid=3))
        h(cb("lb:cphone"))
        h(user_msg(None, mid=4, contact={"phone_number": "+79990000000"}))
        self.assertEqual(leads.get(self.db, USER)["phone"], "+79990000000")
        self.assertIn("+79990000000", self.api.to(OWNER)[-1]["text"])

    def test_free_message_relayed_to_owner(self):
        self.bot.handle(user_msg("/start"))
        self.bot.handle(user_msg("Сколько стоит бот?", mid=9))
        self.assertTrue(any(k["message_id"] == 9 for m, k in self.api.sent if m == "copy"))
        self.assertIn("Передал Максиму", self.api.to(USER)[-1]["text"])

    def test_owner_not_started_falls_back_to_admin_bot(self):
        self.api.fail_owner = True
        self.run_form()
        self.assertTrue(self.said and "Заявка" in self.said[0] and "@slyness_bot" in self.said[0])

    def test_delete_removes_data(self):
        self.run_form()
        self.bot.handle(user_msg("/delete", mid=5))
        self.assertIsNone(leads.get(self.db, USER))
        self.assertIsNone(self.db.one("SELECT * FROM lead_msgs WHERE user_id=?", (USER,)))

    def test_owner_buttons(self):
        self.run_form()
        lead_id = leads.get(self.db, USER)["id"]
        self.bot.handle(cb(f"lo:rej:{lead_id}", uid=OWNER))
        self.assertEqual(leads.get(self.db, USER)["status"], "rejected")

    def test_reminders_once(self):
        self.run_form()
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
        self.db.x("UPDATE leads SET form_at=?", (old,))
        self.assertEqual(self.bot.job(), 1)
        self.assertIn("ждёт ответа", self.api.to(OWNER)[-1]["text"])
        self.assertIsNotNone(self.api.to(OWNER)[-1]["reply_to"])
        self.assertEqual(self.bot.job(), 0)

    def test_abandoned_form_and_magnet_nudges(self):
        h = self.bot.handle
        h(user_msg("/start"))
        h(cb("lb:magnet"))
        h(cb("lb:form"))
        old = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat(timespec="seconds")
        self.db.x("UPDATE leads SET updated_at=?, magnet_at=?", (old, old))
        self.assertEqual(self.bot.job(), 1)   # анкета брошена; статус form — про чек-лист не пишем
        self.assertEqual(self.api.to(USER)[-1]["markup"]["inline_keyboard"][0][0]["callback_data"], "lb:cont")
        self.assertEqual(self.bot.job(), 0)

    def test_stats_and_attribution(self):
        self.run_form()
        text = leads.stats_text(self.db)
        self.assertIn("заявка на разбор 1", text)
        self.assertIn("VK", text)
        users = leads.users_for_attribution(self.db)
        self.assertEqual(users[0]["campaign"], "p12")
        self.assertEqual(users[0]["bought"], 1)
        conv, summary = analyst.conversions(self.cfg, self.db)
        self.assertEqual(conv[12]["bought"], 1.0)
        self.assertIn("оставили заявку", summary)

    def test_owner_can_walk_client_path(self):
        h = self.bot.handle
        h(user_msg("/start", uid=OWNER))
        h(cb("lb:form", uid=OWNER))
        h(cb("lb:sph:0", uid=OWNER))
        h(user_msg("тест задачи", uid=OWNER, mid=2))
        self.assertEqual(leads.get(self.db, OWNER)["task"], "тест задачи")


class CtaLinkTests(unittest.TestCase):
    def test_lead_bot_overrides_cta_url(self):
        cfg = Config(data_dir=Path(tempfile.mkdtemp()), lead_bot_token="x", bot_username="slyness_bot",
                     cta_url="https://t.me/max_karpov", niche="expert")
        self.assertEqual(funnel.cta_link(cfg, "vk", "clip", "p5"), "https://t.me/slyness_bot?start=utm_vk_clip_p5")
        cfg2 = Config(data_dir=Path(tempfile.mkdtemp()), cta_url="https://t.me/max_karpov")
        self.assertEqual(funnel.cta_link(cfg2, "vk", "clip", "p5"), "https://t.me/max_karpov")

    def test_platform_texts_use_lead_bot(self):
        from engine.tgchannel import TGChannel
        from engine.vk import VK
        from engine.youtube import YouTube
        cfg = Config(data_dir=Path(tempfile.mkdtemp()), lead_bot_token="x", bot_username="slyness_bot",
                     cta_url="https://t.me/max_karpov", niche="expert")
        cfg.ensure_dirs()
        db = DB(cfg.db_path)
        pid = db.create_post(status="approved", format="worlds", hook_id="h", theme="t", caption="Текст",
                             script_json=json.dumps({"script": {}}))
        post = db.post(pid)
        tg = TGChannel(cfg, db, None).text_for(post)
        self.assertIn(f"Чек-лист и бесплатный разбор вашей задачи: https://t.me/slyness_bot?start=utm_tg_post_p{pid}", tg)
        title, vk = VK(cfg, db).text_for(post)
        self.assertEqual(title, "Что бизнесу отдать боту")
        self.assertIn(f"utm_vk_clip_p{pid}", vk)
        ytitle, desc = YouTube(cfg, db).text_for(post)
        self.assertTrue(ytitle.startswith("Что бизнесу отдать боту"))
        self.assertIn(f"utm_yt_short_p{pid}", desc)

    def test_privacy_page(self):
        cfg = Config(data_dir=Path(tempfile.mkdtemp()), niche="expert", bot_username="slyness_bot")
        page = leads.privacy_page(cfg)
        self.assertIn("ИП Максим Карпов", page)
        self.assertIn("/delete", page)


if __name__ == "__main__":
    unittest.main()
