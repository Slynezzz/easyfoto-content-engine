"""Бот для заявок эксперта: превращает зрителя ролика в заявку и показывает, какой ролик её привёл.

Человек приходит по ссылке из поста (t.me/<бот>?start=utm_vk_clip_p12 — площадка и номер
ролика) или из шапки профиля (utm_ig_bio). Бот предлагает два шага:
- 📋 чек-лист — бесплатный материал (файл можно заменить из админ-бота: документ с подписью «чеклист»);
- 🧩 бесплатный разбор — 4 коротких вопроса: сфера, задача, что уже пробовали, как связаться.

Владельцу (ADMIN_CHAT_ID) в этом же боте приходит карточка заявки с источником. Ответ на карточку
или на пересланное сообщение (Reply) бот передаёт клиенту — так можно вести переписку, даже если
у клиента нет @ника. Если заявке не ответили сутки — напоминание. Клиенту, бросившему анкету,
и взявшему чек-лист без разбора — по одному мягкому напоминанию. /delete удаляет данные клиента.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from . import funnel, niche
from .config import Config
from .db import DB, now_iso
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger(__name__)

DEFAULT_MAGNET = Path(__file__).with_name("assets") / "Karpov_checklist_12_zadach.pdf"
SPHERES = ["Недвижимость", "Кафе, рестораны, отели", "Салон красоты, wellness", "Сетевой бизнес",
           "Онлайн-школа, эксперт", "Другое"]
STEPS = ("sphere", "task", "tried", "contact")
FORM_NUDGE_AFTER = timedelta(hours=2)
MAGNET_NUDGE_AFTER = timedelta(hours=48)
OWNER_REMIND_AFTER = timedelta(hours=24)
ACK_EVERY_S = 1800  # «передал Максиму» — не чаще раза в полчаса
STATUS_RU = {"new": "пришёл", "magnet": "взял чек-лист", "form": "заполняет анкету", "lead": "🔥 заявка",
             "contacted": "✅ на связи", "rejected": "🚫 не целевой"}
SOURCE_RU = {**funnel.SOURCES, None: "без метки"}


def _profile(cfg: Config) -> dict:
    n = niche.load(cfg.niche)
    return {"author": n.get("lead_author", "автор"),
            "author_gen": n.get("lead_author_gen", n.get("lead_author", "автора")),  # бот Максима Карпова
            "first": n.get("lead_first", "автор"),          # Максим посмотрит
            "first_dat": n.get("lead_first_dat", "автору"),  # передал Максиму
            "first_gen": n.get("lead_first_gen", "автора"),  # заявка у Максима
            "about": n.get("lead_about", ""),
            "contact": n.get("lead_contact", ""),
            "magnet": n.get("lead_magnet_title", "Чек-лист")}


def _ago(iso: str | None, delta: timedelta) -> bool:
    """Прошло ли delta с момента iso."""
    if not iso:
        return False
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - t >= delta


def _kb(rows: list[list[tuple[str, str]]]) -> dict:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


# ------------------------------------------------------------------ данные
def get(db: DB, user_id: int):
    return db.one("SELECT * FROM leads WHERE user_id=?", (user_id,))


def upsert(db: DB, user: dict, start_param: str | None = None):
    """Создаёт или обновляет клиента. Источник — первый (по какой ссылке пришёл впервые)."""
    uid = user["id"]
    name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x).strip() or None
    src = funnel.parse_start(start_param or "")
    row = get(db, uid)
    now = now_iso()
    if not row:
        db.x("INSERT INTO leads(user_id, username, name, source, medium, campaign, post_id, created_at, updated_at) "
             "VALUES (?,?,?,?,?,?,?,?,?)",
             (uid, user.get("username"), name, src["source"], src["medium"], src["campaign"], src["post_id"],
              now, now))
    else:
        db.x("UPDATE leads SET username=COALESCE(?, username), name=COALESCE(?, name), updated_at=? "
             "WHERE user_id=?",
             (user.get("username"), name, now, uid))
        if not row["source"] and src["source"]:
            db.x("UPDATE leads SET source=?, medium=?, campaign=?, post_id=? WHERE user_id=?",
                 (src["source"], src["medium"], src["campaign"], src["post_id"], uid))
    return get(db, uid)


def update(db: DB, user_id: int, **fields) -> None:
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    db.x(f"UPDATE leads SET {cols} WHERE user_id=?", (*fields.values(), user_id))


def users_for_attribution(db: DB) -> list[dict]:
    """Клиенты в формате attribution.attribute: пришёл → взял чек-лист → оставил заявку."""
    return [{"source": r["source"], "campaign": r["campaign"], "registered_at": r["created_at"],
             "photo": 1 if r["magnet_at"] else 0, "bought": 1 if r["form_at"] else 0}
            for r in db.q("SELECT * FROM leads")]


def source_label(db: DB, row) -> str:
    src = SOURCE_RU.get(row["source"], row["source"] or "без метки")
    if row["post_id"]:
        post = db.post(row["post_id"])
        hook = ""
        if post and post["script_json"]:
            try:
                hook = json.loads(post["script_json"]).get("script", {}).get("hook") or ""
            except ValueError:
                hook = ""
        return f"{src}, ролик #{row['post_id']}" + (f" «{hook.replace('*', '')[:70]}»" if hook else "")
    if row["medium"] == "bio":
        return f"{src}, ссылка в профиле"
    return src


def stats_text(db: DB, days: int = 7) -> str:
    rows = db.q("SELECT * FROM leads")
    if not rows:
        return ("Заявок пока нет. Ссылки на бот для заявок с меткой площадки — /links, "
                "ролики сами ведут в бот со своим номером.")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")

    def funnel_line(items) -> str:
        came = len(items)
        magnet = sum(1 for r in items if r["magnet_at"])
        forms = sum(1 for r in items if r["form_at"])
        talk = sum(1 for r in items if r["status"] == "contacted")
        return f"пришли {came} → чек-лист {magnet} → заявка на разбор {forms} → на связи {talk}"

    week = [r for r in rows if r["created_at"] >= since]
    lines = [f"📥 Бот для заявок", f"За {days} дней: {funnel_line(week)}", f"За всё время: {funnel_line(rows)}", ""]
    by_src: dict[str, list] = {}
    for r in rows:
        by_src.setdefault(SOURCE_RU.get(r["source"], r["source"] or "без метки"), []).append(r)
    lines.append("По площадкам:")
    for src, items in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"• {src}: {funnel_line(items)}")
    by_post: dict[int, list] = {}
    for r in rows:
        if r["post_id"]:
            by_post.setdefault(r["post_id"], []).append(r)
    if by_post:
        lines += ["", "Ролики, которые привели больше всех:"]
        top = sorted(by_post.items(), key=lambda kv: (-sum(1 for r in kv[1] if r["form_at"]), -len(kv[1])))[:5]
        for _pid, items in top:
            lines.append(f"• {source_label(db, items[0])}: пришли {len(items)}, "
                         f"заявок {sum(1 for r in items if r['form_at'])}")
    waiting = [r for r in rows if r["status"] == "lead"]
    if waiting:
        lines += ["", f"Ждут ответа: {len(waiting)} — " +
                  ", ".join(f"#{r['id']} {r['name'] or ''}".strip() for r in waiting[:8])]
    return "\n".join(lines)


# ------------------------------------------------------------------ бот
class LeadBot:
    def __init__(self, cfg: Config, db: DB, api: TelegramAPI, fallback_say=None):
        self.cfg, self.db, self.api = cfg, db, api
        self.owner = cfg.admin_chat_id
        self.fallback_say = fallback_say  # админ-бот: если владелец ещё не запускал бот заявок
        self.p = _profile(cfg)

    # ---- служебное
    def setup(self) -> None:
        """Команды и описание бота (описание — только если владелец не задал своё)."""
        try:
            self.api.call("setMyCommands", commands=[
                {"command": "start", "description": "Чек-лист и бесплатный разбор"},
                {"command": "delete", "description": "Удалить мои данные"}])
            current = (self.api.call("getMyDescription") or {}).get("description") or ""
            if not current or "Здесь можно забрать бесплатный чек-лист" in current:  # пустое или наше прежнее
                self.api.call("setMyDescription", description=(
                    f"{self.p['author']} — {self.p['about']}.\n\nЗдесь можно забрать бесплатный чек-лист и "
                    "оставить заявку на бесплатный разбор вашей задачи.")[:512])
            if not (self.api.call("getMyShortDescription") or {}).get("short_description"):
                self.api.call("setMyShortDescription", short_description=(
                    f"{self.p['author']}: чек-лист по автоматизации и бесплатный разбор вашей задачи")[:120])
        except TelegramError as e:
            log.warning("lead bot setup: %s", e)

    def privacy_url(self) -> str:
        return f"{self.cfg.public_base_url.rstrip('/')}/leads-privacy" if self.cfg.public_base_url else ""

    def channel_url(self) -> str:
        ch = str(self.cfg.tg_channel_id or "")
        return f"https://t.me/{ch[1:]}" if ch.startswith("@") else ""

    def poll_once(self, timeout: int = 25) -> int:
        offset = int(self.db.get("lead_tg_offset", "0") or 0) or None
        updates = self.api.get_updates(offset, timeout=timeout)
        for u in updates:
            self.db.set("lead_tg_offset", str(u["update_id"] + 1))
            try:
                self.handle(u)
            except Exception:  # noqa: BLE001 — один клиент не должен ронять бот
                log.exception("lead update %s failed", u.get("update_id"))
        return len(updates)

    def to_owner(self, text: str, markup: dict | None = None, user_id: int | None = None,
                 reply_to: int | None = None) -> int | None:
        """Сообщение владельцу в бот заявок; запоминает, чей это клиент. Не вышло — через админ-бот."""
        if not self.owner:
            return None
        try:
            msg = self.api.send_message(self.owner, text, markup, reply_to=reply_to)
        except TelegramError as e:
            log.warning("lead → owner: %s", e)
            if self.fallback_say:
                uname = self.cfg.bot_username.lstrip("@")
                self.fallback_say(text + f"\n\n⚠️ Открой @{uname} и нажми «Старт» — тогда заявки будут приходить "
                                         "туда, и на них можно будет отвечать клиентам прямо из бота.")
            return None
        if user_id:
            self.db.x("INSERT OR REPLACE INTO lead_msgs(owner_msg_id, user_id) VALUES (?,?)",
                      (msg["message_id"], user_id))
        return msg["message_id"]

    def copy_to_owner(self, user_id: int, message_id: int) -> None:
        try:
            m = self.api.copy_message(self.owner, user_id, message_id)
            self.db.x("INSERT OR REPLACE INTO lead_msgs(owner_msg_id, user_id) VALUES (?,?)",
                      (m["message_id"], user_id))
        except TelegramError as e:
            log.warning("copy to owner: %s", e)

    # ---- маршрутизация
    def handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            if cq["from"]["id"] == self.owner and str(cq.get("data", "")).startswith("lo:"):
                self.on_owner_callback(cq)
            else:
                self.on_callback(cq)
            return
        msg = u.get("message")
        if not msg or msg["chat"].get("type") != "private":
            return
        if msg["from"]["id"] == self.owner and self.owner:
            self.on_owner(msg)
            return
        self.on_user(msg)

    # ---- клиент
    def welcome(self, chat_id: int) -> None:
        text = (f"Здравствуйте! Я бот {self.p['author_gen']}. {self.p['first']} — {self.p['about']}.\n\n"
                f"Здесь можно:\n📋 забрать чек-лист «{self.p['magnet']}» — за 3 минуты станет понятно, "
                "что в вашем бизнесе можно отдать боту;\n"
                f"🧩 оставить заявку на бесплатный разбор — {self.p['first']} лично посмотрит вашу задачу "
                "и подскажет, с чего начать автоматизацию.")
        self.api.send_message(chat_id, text, _kb([[("📋 Забрать чек-лист", "lb:magnet")],
                                                  [("🧩 Бесплатный разбор задачи", "lb:form")],
                                                  [("✍️ Написать напрямую", "lb:write")]]))

    def on_user(self, msg: dict) -> None:
        user, uid = msg["from"], msg["from"]["id"]
        text = (msg.get("text") or "").strip()
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            row = upsert(self.db, user, parts[1] if len(parts) > 1 else None)
            if row["step"]:
                update(self.db, uid, step=None)
            self.welcome(uid)
            return
        if text == "/delete":
            self.db.x("DELETE FROM leads WHERE user_id=?", (uid,))
            self.db.x("DELETE FROM lead_msgs WHERE user_id=?", (uid,))
            self.api.send_message(uid, "Ваши данные удалены. Если захотите вернуться — нажмите /start.")
            return
        if text in ("/menu", "/help"):
            upsert(self.db, user)
            self.welcome(uid)
            return
        row = upsert(self.db, user)
        step = row["step"]
        if step == "contact" and msg.get("contact"):
            update(self.db, uid, phone=msg["contact"].get("phone_number"), contact="телефон")
            self.finish(uid)
            return
        if step in STEPS:
            self.answer(uid, step, msg, text)
            return
        self.relay_from_user(msg, row)

    def answer(self, uid: int, step: str, msg: dict, text: str) -> None:
        value = text
        if not text:  # голосовое, фото, файл — перешлём владельцу вместе с карточкой
            extra = json.loads(get(self.db, uid)["extra_msgs"] or "[]") + [msg["message_id"]]
            update(self.db, uid, extra_msgs=json.dumps(extra))
            value = "(голосовое или файл — пересылаю ниже)"
        if step == "sphere":
            update(self.db, uid, sphere=value[:200])
            self.ask(uid, "task")
        elif step == "task":
            update(self.db, uid, task=value[:1500])
            self.ask(uid, "tried")
        elif step == "tried":
            update(self.db, uid, tried=value[:800])
            self.ask(uid, "contact")
        elif step == "contact":
            update(self.db, uid, contact=value[:300])
            self.finish(uid)

    def ask(self, uid: int, step: str) -> None:
        update(self.db, uid, step=step)
        if step == "sphere":
            rows = [[(s, f"lb:sph:{i}")] for i, s in enumerate(SPHERES)]
            self.api.send_message(uid, "Вопрос 1 из 4. В какой сфере ваш бизнес?", _kb(rows))
        elif step == "task":
            self.api.send_message(uid, "Вопрос 2 из 4. Что сейчас отнимает больше всего времени или где теряются "
                                       "клиенты? Опишите своими словами — можно голосовым.")
        elif step == "tried":
            self.api.send_message(uid, "Вопрос 3 из 4. Пробовали уже что-то автоматизировать? Что не устроило?",
                                  _kb([[("Пока ничего не пробовали", "lb:tried0")]]))
        elif step == "contact":
            self.api.send_message(uid, f"Вопрос 4 из 4. Как {self.p['first_dat']} удобнее с вами связаться?",
                                  _kb([[("💬 Пишите сюда, в Telegram", "lb:ctg")],
                                       [("📞 Оставлю телефон", "lb:cphone")]]))

    def finish(self, uid: int) -> None:
        row = get(self.db, uid)
        update(self.db, uid, step=None, status="lead", form_at=row["form_at"] or now_iso())
        first = self.p["first"]
        tail = f"\n\nПока ждёте — разборы и кейсы в канале: {self.channel_url()}" if self.channel_url() else ""
        self.api.send_message(uid, f"Спасибо! {first} посмотрит вашу задачу и напишет вам лично — обычно в течение "
                                   f"дня.{tail}", {"remove_keyboard": True})
        self.send_card(get(self.db, uid))

    def send_card(self, row) -> None:
        who = escape(row["name"] or "без имени")
        nick = f"@{row['username']}" if row["username"] else "без @ника — отвечайте через Reply здесь"
        contact = row["contact"] or "—"
        if row["phone"]:
            contact = f"телефон {row['phone']}"
        text = (f"🔥 Заявка на бесплатный разбор #{row['id']}\n"
                f"{who} ({nick})\n\n"
                f"Сфера: {row['sphere'] or '—'}\n"
                f"Задача: {row['task'] or '—'}\n"
                f"Пробовали: {row['tried'] or '—'}\n"
                f"Связь: {contact}\n"
                f"Откуда: {source_label(self.db, row)}\n\n"
                "Ответьте на это сообщение (Reply) — я передам ответ клиенту.")
        mid = self.to_owner(text, _kb([[("✅ Связался", f"lo:done:{row['id']}"),
                                        ("🚫 Не целевой", f"lo:rej:{row['id']}")]]), user_id=row["user_id"])
        if mid:
            for m in json.loads(row["extra_msgs"] or "[]"):
                self.copy_to_owner(row["user_id"], m)
            update(self.db, row["user_id"], extra_msgs=None)

    def relay_from_user(self, msg: dict, row) -> None:
        uid = msg["from"]["id"]
        who = row["name"] or "клиент"
        nick = f" @{row['username']}" if row["username"] else ""
        st = STATUS_RU.get(row["status"], row["status"])
        if self.to_owner(f"💬 {who}{nick} ({st}, {source_label(self.db, row)}):", user_id=uid):
            self.copy_to_owner(uid, msg["message_id"])
        key = f"lead_ack:{uid}"
        last = float(self.db.get(key, "0") or 0)
        if time.time() - last > ACK_EVERY_S:
            self.db.set(key, str(time.time()))
            self.api.send_message(uid, f"Передал {self.p['first_dat']} — он ответит здесь же.")

    def send_magnet(self, uid: int) -> None:
        path = Path(self.db.get("lead_magnet_file") or "")
        if not path.is_file():
            path = DEFAULT_MAGNET
        title = self.db.get("lead_magnet_title") or self.p["magnet"]
        self.api.send_document(uid, path, f"📋 {title}")
        row = get(self.db, uid)
        update(self.db, uid, magnet_at=row["magnet_at"] or now_iso(),
               status="magnet" if row["status"] == "new" else row["status"])
        first = self.p["first"]
        self.api.send_message(uid, "Отметьте пункты, которые у вас сейчас делаются вручную. Если набралось три и "
                                   "больше — есть с чего начать.\n\n"
                                   f"Хотите, {first} бесплатно разберёт ваш процесс и подскажет, что автоматизировать "
                                   "в первую очередь?",
                              _kb([[("🧩 Да, хочу разбор", "lb:form")]]))

    def start_form(self, uid: int) -> None:
        row = get(self.db, uid)
        if row["status"] in ("lead", "contacted"):
            self.api.send_message(uid, f"Ваша заявка уже у {self.p['first_gen']} — он напишет вам. Если хотите что-то добавить, "
                                       "просто напишите сюда.")
            return
        policy = f" (политика: {self.privacy_url()})" if self.privacy_url() else ""
        update(self.db, uid, status="form" if row["status"] in ("new", "magnet") else row["status"])
        self.api.send_message(uid, "Четыре коротких вопроса — пара минут. Отвечая, вы соглашаетесь на обработку "
                                   f"ваших ответов и контактов, чтобы с вами связаться{policy}. "
                                   "Удалить данные можно командой /delete.")
        self.ask(uid, "sphere")

    def on_callback(self, cq: dict) -> None:
        user, uid, data = cq["from"], cq["from"]["id"], cq.get("data", "")
        self.api.answer_callback(cq["id"])
        row = upsert(self.db, user)
        if data == "lb:magnet":
            self.send_magnet(uid)
        elif data == "lb:form":
            self.start_form(uid)
        elif data == "lb:cont":
            if row["step"]:
                self.ask(uid, row["step"])
            else:
                self.start_form(uid)
        elif data == "lb:write":
            contact = f" Или напрямую: {self.p['contact']}." if self.p["contact"] else ""
            self.api.send_message(uid, f"Напишите сообщение здесь — я передам его {self.p['first_dat']}, "
                                       f"он ответит в этом чате.{contact}")
        elif data.startswith("lb:sph:") and row["step"] == "sphere":
            i = int(data.split(":")[2])
            if SPHERES[i] == "Другое":
                self.api.send_message(uid, "Напишите вашу сферу одним сообщением.")
                return
            update(self.db, uid, sphere=SPHERES[i])
            self.ask(uid, "task")
        elif data == "lb:tried0" and row["step"] == "tried":
            update(self.db, uid, tried="пока ничего")
            self.ask(uid, "contact")
        elif data == "lb:ctg" and row["step"] == "contact":
            update(self.db, uid, contact="пишите в Telegram")
            self.finish(uid)
        elif data == "lb:cphone" and row["step"] == "contact":
            self.api.send_message(uid, "Нажмите кнопку ниже — Telegram отправит ваш номер. Или напишите его сообщением.",
                                  {"keyboard": [[{"text": "📞 Отправить номер", "request_contact": True}]],
                                   "resize_keyboard": True, "one_time_keyboard": True})

    # ---- владелец
    def on_owner(self, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        reply = msg.get("reply_to_message")
        if reply:
            m = self.db.one("SELECT user_id FROM lead_msgs WHERE owner_msg_id=?", (reply["message_id"],))
            if m:
                try:
                    self.api.copy_message(m["user_id"], self.owner, msg["message_id"])
                except TelegramError as e:
                    self.api.send_message(self.owner, f"⚠️ Не доставлено: {e}. Возможно, клиент остановил бота.")
                    return
                row = get(self.db, m["user_id"])
                if row and row["status"] == "lead":
                    update(self.db, m["user_id"], status="contacted", contacted_at=now_iso())
                self.api.send_message(self.owner, "✉️ Отправлено клиенту.")
                return
        if text.startswith("/leads") or text.startswith("/stats"):
            self.api.send_message(self.owner, stats_text(self.db))
            return
        # владелец может пройти путь клиента сам: /start, анкета, /delete
        own = get(self.db, self.owner)
        if text.startswith("/start") or text == "/delete" or msg.get("contact") or (own and own["step"]):
            self.on_user(msg)
            return
        self.api.send_message(self.owner, "Сюда приходят заявки и сообщения клиентов. Чтобы ответить клиенту — "
                                          "ответьте (Reply) на его карточку или сообщение. /leads — сводка.\n\n"
                                          "Так бот видят клиенты:")
        self.welcome(self.owner)

    def on_owner_callback(self, cq: dict) -> None:
        _, action, lead_id = cq["data"].split(":")
        row = self.db.one("SELECT * FROM leads WHERE id=?", (int(lead_id),))
        if not row:
            self.api.answer_callback(cq["id"], "Заявка удалена клиентом")
            return
        if action == "done":
            update(self.db, row["user_id"], status="contacted", contacted_at=row["contacted_at"] or now_iso())
            label = "✅ На связи"
        else:
            update(self.db, row["user_id"], status="rejected")
            label = "🚫 Не целевой"
        self.api.answer_callback(cq["id"], "Отмечено")
        self.api.edit_markup(self.owner, cq["message"]["message_id"],
                             {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]})

    # ---- напоминания (раз в 10 минут из движка)
    def job(self) -> int:
        done = 0
        first = self.p["first"]
        for r in self.db.q("SELECT * FROM leads WHERE step IS NOT NULL AND (nudged & 1) = 0"):
            if _ago(r["updated_at"], FORM_NUDGE_AFTER):
                self._nudge(r, 1, "Вы начали заявку на бесплатный разбор — осталось ответить на пару вопросов.",
                            _kb([[("Продолжить", "lb:cont")]]))
                done += 1
        for r in self.db.q("SELECT * FROM leads WHERE status='magnet' AND form_at IS NULL AND (nudged & 2) = 0"):
            if _ago(r["magnet_at"], MAGNET_NUDGE_AFTER):
                self._nudge(r, 2, "Удалось посмотреть чек-лист? Если какие-то пункты отозвались — "
                                  f"{first} бесплатно разберёт ваш процесс и подскажет, с чего начать.",
                            _kb([[("🧩 Хочу разбор", "lb:form")]]))
                done += 1
        for r in self.db.q("SELECT * FROM leads WHERE status='lead' AND reminded=0"):
            if _ago(r["form_at"], OWNER_REMIND_AFTER):
                card = self.db.one("SELECT MIN(owner_msg_id) m FROM lead_msgs WHERE user_id=?", (r["user_id"],))
                self.to_owner(f"⏰ Заявка #{r['id']} ({r['name'] or 'клиент'}) ждёт ответа больше суток.",
                              user_id=r["user_id"], reply_to=card["m"] if card else None)
                self.db.x("UPDATE leads SET reminded=1 WHERE id=?", (r["id"],))
                done += 1
        return done

    def _nudge(self, r, bit: int, text: str, markup: dict) -> None:
        self.db.x("UPDATE leads SET nudged = nudged | ? WHERE id=?", (bit, r["id"]))
        try:
            self.api.send_message(r["user_id"], text, markup)
        except TelegramError as e:  # клиент остановил бота — больше не пишем
            log.info("nudge %s: %s", r["user_id"], e)


def privacy_page(cfg: Config) -> str:
    p = _profile(cfg)
    bot = cfg.bot_username.lstrip("@")
    return ("<h2>Политика обработки персональных данных бота для заявок</h2>"
            f"<p>Оператор: ИП {escape(p['author'])}. Бот: <a href='https://t.me/{bot}'>@{bot}</a>.</p>"
            "<p><b>Какие данные.</b> Имя и @ник в Telegram, числовой идентификатор Telegram, ответы на вопросы "
            "анкеты (сфера бизнеса, задача, опыт автоматизации), номер телефона — только если вы сами его "
            "отправили, сообщения, которые вы пишете боту, и ссылка, по которой вы пришли.</p>"
            "<p><b>Зачем.</b> Чтобы связаться с вами по вашей заявке, подготовить бесплатный разбор и понимать, "
            "какие материалы полезны. Данные не продаются и не передаются третьим лицам.</p>"
            "<p><b>Хранение.</b> В закрытой базе на сервере оператора, пока вы их не удалите.</p>"
            "<p><b>Удаление.</b> Команда /delete в боте удаляет ваши данные сразу. Также можно написать оператору"
            + (f": {escape(p['contact'])}" if p["contact"] else "") + ".</p>")
