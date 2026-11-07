"""Кодовые слова в комментариях Instagram: «напишите БОТ — пришлю чек-лист».

Каждое слово — свой материал (настраивается из админ-бота за одно сообщение):
- magnet — текущий чек-лист бота для заявок;
- file   — свой файл (PDF, DOCX, картинка), прислан в админ-бот с подписью «слово БОТ Название»;
- link   — ссылка (гайд, статья, запись эфира);
- text   — короткий текст.

Раз в несколько минут движок читает свежие комментарии под своими публикациями Instagram (без вебхуков:
так работает и без отдельной настройки в Meta). Нашёл кодовое слово — отвечает автору комментария
в директ (private reply, один раз на комментарий) и коротко под комментарием. Файлы и чек-лист выдаёт
бот для заявок по ссылке с меткой слова и поста (k5p12) — так видно, какое слово и какой пост привели человека.
То же слово, написанное прямо в бот для заявок, тоже выдаёт материал.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import funnel
from .config import Config
from .db import DB, now_iso

log = logging.getLogger(__name__)

KINDS = ("magnet", "file", "link", "text")
KIND_RU = {"magnet": "чек-лист бота", "file": "файл", "link": "ссылка", "text": "текст"}
WORD_RE = re.compile(r"^[0-9A-Za-zА-Яа-яЁё]{2,24}$")
SCAN_DAYS = 7           # ответ в директ возможен на комментарий не старше 7 дней — старее не смотрим
PRIVATE_REPLY_DAYS = 7  # Instagram разрешает ответ в директ на комментарий не старше 7 дней
PUBLIC_REPLY = "Отправил в директ 📩"



class KeywordError(ValueError):
    pass


def norm(text: str) -> str:
    return (text or "").upper().replace("Ё", "Е")


def all_(db: DB) -> list:
    return db.q("SELECT * FROM keywords ORDER BY id")


def get(db: DB, kid: int):
    return db.one("SELECT * FROM keywords WHERE id=?", (kid,))


def find_in(db: DB, text: str):
    """Кодовое слово, которое встречается в тексте отдельным словом (регистр и ё/е не важны)."""
    words = set(re.findall(r"[0-9A-ZА-Я]+", norm(text)))
    for k in all_(db):
        if norm(k["word"]) in words:
            return k
    return None


def add(db: DB, word: str, title: str = "", kind: str = "magnet", payload: str = "") -> int:
    word = (word or "").strip().strip("«»\"'")
    if not WORD_RE.match(word):
        raise KeywordError("кодовое слово — одно слово из букв или цифр, 2–24 символа (например, БОТ или ЧЕКЛИСТ)")
    if kind not in KINDS:
        raise KeywordError(f"неизвестный тип «{kind}»")
    if kind == "link" and not re.match(r"^https?://\S+$", payload or ""):
        raise KeywordError("для ссылки нужен адрес, начинающийся с https://")
    title = (title or "").strip()[:120] or {"magnet": "Чек-лист", "file": "Материал", "link": "Материал",
                                              "text": "Ответ"}[kind]
    existing = db.one("SELECT id FROM keywords WHERE upper(replace(word, 'ё', 'е'))=?", (norm(word),))
    if existing:
        db.x("UPDATE keywords SET word=?, title=?, kind=?, payload=? WHERE id=?",
             (word.upper(), title, kind, payload, existing["id"]))
        return existing["id"]
    return db.x("INSERT INTO keywords(word, title, kind, payload, created_at) VALUES (?,?,?,?,?)",
                (word.upper(), title, kind, payload, now_iso()))


def remove(db: DB, word_or_id: str) -> bool:
    row = (db.one("SELECT * FROM keywords WHERE id=?", (int(word_or_id),)) if str(word_or_id).isdigit()
           else db.one("SELECT * FROM keywords WHERE upper(replace(word, 'ё', 'е'))=?", (norm(word_or_id),)))
    if not row:
        return False
    if row["kind"] == "file" and row["payload"]:
        Path(row["payload"]).unlink(missing_ok=True)
    db.x("DELETE FROM keywords WHERE id=?", (row["id"],))
    return True


def store_file(cfg: Config, src: Path, word: str) -> Path:
    folder = cfg.data_dir / "keywords"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{norm(word).lower()}{src.suffix.lower()}"
    shutil.move(str(src), dest)
    return dest


def bot_link(cfg: Config, kid: int, post_id: int | None = None) -> str:
    return funnel.link(cfg, "ig", "kw", f"k{kid}" + (f"p{post_id}" if post_id else ""))


def dm_text(cfg: Config, k, post_id: int | None = None) -> str:
    """Текст в директ. Файлы и чек-лист — через бот для заявок (там же — бесплатный разбор)."""
    if k["kind"] == "link":
        return f"Привет! Как обещал — «{k['title']}»: {k['payload']}"
    if k["kind"] == "text":
        return f"Привет! {k['payload']}"[:950] + (f"\n\nЕщё материалы — в Telegram: {bot_link(cfg, k['id'], post_id)}"
                                                  if cfg.lead_bot_token else "")
    return f"Привет! Держите «{k['title']}» — заберите в Telegram, бот пришлёт сразу: {bot_link(cfg, k['id'], post_id)}"


def list_text(db: DB) -> str:
    rows = all_(db)
    lines = ["🔑 Кодовые слова в комментариях Instagram"]
    if not rows:
        lines.append("\nПока нет ни одного.")
    for k in rows:
        what = KIND_RU[k["kind"]]
        if k["kind"] in ("link", "text"):
            what += f": {k['payload'][:60]}"
        lines.append(f"\n• {k['word']} → «{k['title']}» ({what}) · сработало {k['uses']}")
    lines.append("\nДобавить или изменить:\n"
                 "• /keyword БОТ — выдаёт чек-лист бота для заявок\n"
                 "• /keyword ГАЙД Гайд по ботам | https://ссылка — выдаёт ссылку\n"
                 "• /keyword СКИДКА Ответ | Текст, который придёт в директ — выдаёт текст\n"
                 "• файл (PDF, DOCX, картинка) с подписью «слово РАЗБОР Название» — выдаёт этот файл\n"
                 "Удалить — кнопкой ниже или /keyword - БОТ. Активные слова сценарист сам вставляет в призыв "
                 "постов для Instagram.")
    return "\n".join(lines)


def parse_command(args: list[str]) -> tuple[str, str, str, str]:
    """/keyword СЛОВО [Название] [| ссылка или текст] → (word, title, kind, payload)."""
    if not args:
        raise KeywordError("напишите слово: /keyword БОТ")
    word, rest = args[0], " ".join(args[1:]).strip()
    title, payload = (rest.split("|", 1) + [""])[:2] if "|" in rest else (rest, "")
    title, payload = title.strip(), payload.strip()
    if not payload:
        return word, title, "magnet", ""
    if re.match(r"^https?://\S+$", payload):
        return word, title, "link", payload
    return word, title, "text", payload[:900]


def prompt_hint(db: DB) -> str:
    """Подсказка сценаристу: активные кодовые слова для призыва в Instagram."""
    rows = all_(db)
    if not rows:
        return ""
    items = "; ".join(f"{k['word']} → «{k['title']}»" for k in rows[:5])
    return ("КОДОВЫЕ СЛОВА (Instagram): " + items + ". В посте для Instagram призыв делай через подходящее по смыслу "
            "слово: «Напишите СЛОВО в комментариях — пришлю … в директ». Слово пиши заглавными буквами.")


# ------------------------------------------------------------------ комментарии
def _ago(ts: str | None) -> timedelta:
    try:
        t = datetime.fromisoformat((ts or "").replace("+0000", "+00:00"))
    except ValueError:
        return timedelta(0)
    return datetime.now(timezone.utc) - t


def scan(cfg: Config, db: DB, ig) -> dict:
    """Проверяет свежие комментарии и отвечает на кодовые слова. {"replied", "errors"}.

    Экономно к лимитам Instagram: один запрос — сколько комментариев у последних публикаций;
    сами комментарии читаются только там, где их стало больше. Видит и публикации, выложенные вручную.
    """
    out = {"replied": 0, "errors": []}
    if not all_(db) or not ig.ready():
        return out
    me = (db.get("ig_username") or "").lower()
    try:
        media = ig._req("GET", f"{ig.account_id}/media", fields="id,comments_count,timestamp",
                        limit="25").get("data", [])
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"список публикаций: {e}")
        return out
    for m in media:
        mid, count = str(m.get("id") or ""), int(m.get("comments_count") or 0)
        if not mid or _ago(m.get("timestamp")) > timedelta(days=SCAN_DAYS):
            continue
        key = f"kwc:{mid}"
        if count <= int(db.get(key, "0") or 0):
            continue
        post = db.one("SELECT id FROM posts WHERE ig_media_id=?", (mid,))
        post_id = post["id"] if post else None
        try:
            comments = ig._req("GET", f"{mid}/comments", fields="id,text,username,timestamp",
                               limit="50").get("data", [])
        except Exception as e:  # noqa: BLE001 — одна публикация не должна останавливать остальные
            out["errors"].append(f"комментарии {mid}: {e}")
            continue
        for c in comments:
            cid = str(c.get("id") or "")
            if not cid or (c.get("username") or "").lower() == me:
                continue
            if db.one("SELECT 1 FROM kw_events WHERE comment_id=?", (cid,)):
                continue
            k = find_in(db, c.get("text") or "")
            if not k:
                continue
            if _ago(c.get("timestamp")) > timedelta(days=PRIVATE_REPLY_DAYS):
                db.x("INSERT OR IGNORE INTO kw_events(comment_id, keyword_id, post_id, username, status, created_at) "
                     "VALUES (?,?,?,?,?,?)", (cid, k["id"], post_id, c.get("username"), "expired", now_iso()))
                continue
            status, err = "sent", None
            try:
                ig._req("POST", f"{ig.account_id}/messages",
                        recipient=json.dumps({"comment_id": cid}),
                        message=json.dumps({"text": dm_text(cfg, k, post_id)}, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                status, err = "error", str(e)[:300]
                out["errors"].append(f"{k['word']} от @{c.get('username')}: {e}")
            db.x("INSERT OR IGNORE INTO kw_events(comment_id, keyword_id, post_id, username, status, error, created_at) "
                 "VALUES (?,?,?,?,?,?,?)", (cid, k["id"], post_id, c.get("username"), status, err, now_iso()))
            if status == "sent":
                db.x("UPDATE keywords SET uses = uses + 1 WHERE id=?", (k["id"],))
                out["replied"] += 1
                try:
                    ig._req("POST", f"{cid}/replies", message=PUBLIC_REPLY)
                except Exception as e:  # noqa: BLE001 — директ ушёл, ответ под комментарием не критичен
                    log.info("public reply %s: %s", cid, e)
        db.set(key, str(count))  # неудачные ответы записаны в kw_events и придут владельцу в отчёте
    return out
