"""Админ-бот в Telegram: одобрение роликов, загрузка фото, команды.

Отвечает только владельцу (ADMIN_CHAT_ID). Пока ID не задан, на любое
сообщение присылает chat_id — его нужно вписать в переменные Railway.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import images
from .config import Config
from .db import DB
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger(__name__)

FORMAT_RU = {"before_after": "было → стало", "looks_carousel": "карусель образов", "worlds": "разные миры"}

HELP = """Команды:
/make — собрать ролик прямо сейчас
/queue — что ждёт одобрения и публикации
/status — состояние системы
/ai — проверить ключи и модели нейросетей
/models — какие модели есть у посредников
/trends — обновить тренды прямо сейчас
/stats — статистика роликов
/analyze — пересчитать плейбук по статистике
/pause и /resume — остановить или включить автопубликацию
/scan — разобрать новые фото
/move 5 до — перенести 5 последних фото в селфи «до» (или «реф», «библиотека»)

Фото: просто пришли их сюда (лучше файлом, без сжатия).
Подпись к фото или альбому:
• без подписи — в библиотеку готовых нейрофото
• «до» — обычные селфи для формата «было → стало»
• «реф» — референсы для генерации новых кадров

Ошибся папкой? Ответь на своё фото словом «до», «реф» или «библиотека»."""


@dataclass
class Services:
    """Действия, которые бот вызывает у остальной системы."""
    make_one: Callable[[], int]
    instagram_ready: Callable[[], bool]
    run_analysis: Callable[[], str]
    stats_text: Callable[[], str]
    check_ai: Callable[[], str] = lambda: "Проверка нейросетей недоступна"
    scout_trends: Callable[[], str] = lambda: "Разведчик трендов недоступен"
    models_report: Callable[[], str] = lambda: "Список моделей недоступен"


def approval_keyboard(post_id: int) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ В расписание", "callback_data": f"ok:{post_id}"},
         {"text": "⚡ Сейчас", "callback_data": f"now:{post_id}"}],
        [{"text": "🔁 Другой вариант", "callback_data": f"redo:{post_id}"},
         {"text": "❌ Отклонить", "callback_data": f"no:{post_id}"}],
        [{"text": "📝 Текст поста", "callback_data": f"txt:{post_id}"}],
    ]}


def retry_keyboard(post_id: int) -> dict:
    return {"inline_keyboard": [[{"text": "🔁 Повторить публикацию", "callback_data": f"retry:{post_id}"}]]}


def _status_keyboard(label: str) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]}


class AdminBot:
    def __init__(self, cfg: Config, db: DB, api: TelegramAPI, services: Services,
                 run_bg: Callable[[Callable[[], None]], object] | None = None):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.s = services
        # Сборка и публикация идут минуты: на проде выполняем их в отдельном
        # потоке, чтобы бот продолжал отвечать. В тестах — синхронно.
        self.run_bg = run_bg or (lambda fn: fn())
        self._album_kind: dict[str, str] = {}

    # ------------------------------------------------------------ helpers
    @property
    def admin(self) -> int:
        return self.cfg.admin_chat_id

    def say(self, text: str, markup: dict | None = None) -> None:
        if not self.admin:
            return
        try:
            self.api.send_message(self.admin, text, markup)
        except TelegramError as e:
            log.error("send_message: %s", e)

    def _make(self) -> int | None:
        """Собирает ролик; при ошибке объясняет причину владельцу."""
        try:
            return self.s.make_one()
        except Exception as e:  # noqa: BLE001
            log.exception("make_one failed")
            self.say(f"⚠️ Не получилось собрать ролик: {e}")
            return None

    def _publish_asap(self, cq: dict, post_id: int, mark: Callable[[str], None]) -> None:
        """Ставит одобренный ролик на текущее время. Публикует его планировщик —
        он единственный, кто публикует, поэтому двойной публикации не бывает."""
        now = datetime.now(ZoneInfo(self.cfg.timezone)).strftime("%Y-%m-%d %H:%M")
        self.db.update_post(post_id, scheduled_for=now)
        if self.paused:
            self.api.answer_callback(cq["id"], "Автопубликация на паузе")
            mark("✅ Выйдет после /resume")
            return
        self.api.answer_callback(cq["id"], "Опубликую в течение минуты")
        mark("⏳ Публикуется…")

    @property
    def paused(self) -> bool:
        return self.db.get("paused") == "1"

    # ----------------------------------------------------------- approval
    def send_for_approval(self, post_id: int) -> bool:
        if not self.admin or not self.db.claim_post(post_id, "rendered", "sending"):
            return False
        post = self.db.post(post_id)
        plan = json.loads(post["script_json"])
        tags = " · ".join(x for x in (
            FORMAT_RU.get(post["format"], post["format"]),
            f"тема: {post['theme']}",
            f"хук: {post['hook_id']}",
            f"стиль: {post['style']}" if post["style"] else "",
            "эксперимент" if post["explore"] else "",
        ) if x)
        when = f"\nПубликация: {post['scheduled_for']}" if post["scheduled_for"] else ""
        warn = ""
        if plan.get("llm_ok") is False:
            warn = (f"⚠️ Нейросеть не ответила, тексты по запасному шаблону.\n"
                    f"Причина: {(plan.get('llm_error') or 'неизвестна')[:200]}\n\n")
        caption = (f"{warn}#{post_id} · {tags}{when}\n\n"
                   f"Хук: {plan['script']['hook'].replace('*', '')}\n\n{plan['post_caption'][:600]}")
        try:
            msg = self.api.send_video(self.admin, post["video_path"], caption,
                                      approval_keyboard(post_id), cover=post["cover_path"])
        except Exception as e:  # noqa: BLE001 — вернём в очередь, досылка попробует снова
            log.error("send_video #%s: %s", post_id, e)
            self.db.update_post(post_id, status="rendered")
            return False
        self.db.update_post(post_id, status="pending", approval_message_id=msg["message_id"])
        return True

    def push_rendered(self) -> int:
        return sum(self.send_for_approval(p["id"]) for p in self.db.posts("rendered"))

    # ------------------------------------------------------------ updates
    def poll_once(self, timeout: int = 25) -> int:
        offset = int(self.db.get("tg_offset", "0") or 0) or None
        updates = self.api.get_updates(offset, timeout=timeout)
        for u in updates:
            # смещение сохраняем до обработки: упавшее обновление не зациклит бота
            self.db.set("tg_offset", str(u["update_id"] + 1))
            try:
                self.handle(u)
            except Exception:  # noqa: BLE001 — бот не должен падать из-за одного сообщения
                log.exception("update %s failed", u.get("update_id"))
                self.say("⚠️ Ошибка при обработке, подробности в логах Railway.")
        return len(updates)

    def handle(self, u: dict) -> None:
        if "callback_query" in u:
            cq = u["callback_query"]
            if cq["from"]["id"] != self.admin:
                self.api.answer_callback(cq["id"])
                return
            self.on_callback(cq)
            return
        msg = u.get("message")
        if not msg or msg["chat"].get("type") != "private":
            return
        chat_id = msg["chat"]["id"]
        if not self.admin:
            self.api.send_message(chat_id, f"Ваш chat_id: {chat_id}\nВпишите его в переменную ADMIN_CHAT_ID на Railway и перезапустите сервис.")
            return
        if chat_id != self.admin:
            return
        if msg.get("photo") or msg.get("document"):
            self.on_file(msg)
            return
        text = (msg.get("text") or "").strip()
        if text.startswith("/"):
            parts = text.split()
            self.on_command(parts[0].split("@")[0].lower(), parts[1:])
        elif msg.get("reply_to_message") and text:
            self.on_reply(text, msg["reply_to_message"])

    # ------------------------------------------------------------ files
    def _kind_from_caption(self, msg: dict) -> str:
        cap = (msg.get("caption") or "").strip().lower()
        group = msg.get("media_group_id")
        if cap.startswith(("до", "before", "селфи")):
            kind = "before"
        elif cap.startswith(("реф", "ref")):
            kind = "refs"
        elif cap or not group:
            kind = "library"
        else:
            kind = self._album_kind.get(group, "library")  # подпись бывает только у первого фото альбома
        if group and cap:
            self._album_kind[group] = kind
        return kind

    def on_file(self, msg: dict) -> None:
        kind = self._kind_from_caption(msg)
        if msg.get("photo"):
            file_id, ext = msg["photo"][-1]["file_id"], ".jpg"
        else:
            doc = msg["document"]
            name = (doc.get("file_name") or "").lower()
            ext = Path(name).suffix or ".jpg"
            if ext not in images.IMAGE_EXT:
                self.say("Это не фото. Принимаю jpg, png, webp.")
                return
            if (doc.get("file_size") or 0) > 20 * 1024 * 1024:
                self.say("Файл больше 20 МБ — Telegram не даст его скачать боту. Сожми и пришли снова.")
                return
            file_id = doc["file_id"]
        dest = self.cfg.assets_dir / kind / f"{uuid.uuid4().hex[:12]}{ext}"
        self.api.download(file_id, dest)
        images.scan(self.cfg, self.db)
        # запоминаем сообщение, чтобы фото можно было перенести ответом на него
        self.db.x("UPDATE assets SET tg_message_id=? WHERE path=?", (msg.get("message_id"), str(dest)))
        total = len(self.db.assets("ref" if kind == "refs" else kind))
        label = {"library": "библиотеке", "before": "селфи «до»", "refs": "референсах"}[kind]
        # на альбом отвечаем один раз, а не на каждое фото
        if not msg.get("media_group_id") or msg.get("caption") is not None or total % 5 == 0:
            self.say(f"Сохранил. Сейчас в {label}: {total}.")

    # ------------------------------------------------------- moving files
    @staticmethod
    def parse_kind(word: str) -> str | None:
        w = word.strip().lower().strip(".!«»\"")
        if w.startswith(("до", "before", "селфи")):
            return "before"
        if w.startswith(("реф", "ref")):
            return "refs"
        if w.startswith(("библ", "lib", "после")):
            return "library"
        return None

    def move_asset(self, row, kind: str) -> bool:
        """Переносит фото в другую папку (библиотека / до / реф)."""
        db_kind = "ref" if kind == "refs" else kind
        if row["kind"] == db_kind:
            return False
        src = Path(row["path"])
        dest = self.cfg.assets_dir / kind / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            src.replace(dest)
        # В библиотеке фото нужна разметка; в других папках она не используется.
        self.db.x("UPDATE assets SET path=?, kind=?, title=NULL, theme=NULL, description=NULL, "
                  "tag_attempts=0 WHERE id=?", (str(dest), db_kind, row["id"]))
        return True

    def _counts_text(self) -> str:
        return (f"Библиотека: {len(self.db.assets('library'))}, селфи «до»: {len(self.db.assets('before'))}, "
                f"референсы: {len(self.db.assets('ref'))}.")

    def on_reply(self, text: str, replied: dict) -> None:
        kind = self.parse_kind(text)
        if not kind:
            self.say("Чтобы перенести фото, ответь на него одним словом: «до», «реф» или «библиотека».")
            return
        row = self.db.one("SELECT * FROM assets WHERE tg_message_id=?", (replied.get("message_id"),))
        if not row:
            self.say("Не нашёл это фото. Отвечать нужно на своё сообщение с фото, "
                     "или используй /move — например, /move 5 до.")
            return
        moved = self.move_asset(row, kind)
        self.say(("Перенёс. " if moved else "Оно уже там. ") + self._counts_text())

    def move_last(self, args: list[str]) -> None:
        n = next((int(a) for a in args if a.isdigit()), 0)
        kind = next((k for k in map(self.parse_kind, args) if k), None)
        if not n or not kind:
            self.say("Как пользоваться: /move 5 до — перенести 5 последних загруженных фото "
                     "в селфи «до». Вместо «до» можно «реф» или «библиотека».")
            return
        rows = self.db.q("SELECT * FROM assets WHERE kind IN ('library','before','ref') "
                         "ORDER BY id DESC LIMIT ?", (min(n, 50),))
        moved = sum(self.move_asset(r, kind) for r in rows)
        self.say(f"Перенёс {moved} фото. " + self._counts_text())

    # ---------------------------------------------------------- commands
    def on_command(self, cmd: str, args: list[str] | None = None) -> None:
        args = args or []
        if cmd in ("/start", "/help"):
            self.say(HELP)
        elif cmd == "/make":
            self.say("Собираю ролик, это займёт 1–3 минуты…")

            def job():
                post_id = self._make()
                if post_id is not None and not self.send_for_approval(post_id):
                    self.say(f"Ролик #{post_id} собран, отправлю его чуть позже.")
            self.run_bg(job)
        elif cmd == "/queue":
            self.say(self.queue_text())
        elif cmd == "/status":
            self.say(self.status_text())
        elif cmd == "/stats":
            self.say(self.s.stats_text())
        elif cmd == "/analyze":
            self.run_bg(lambda: self.say(self.s.run_analysis()))
        elif cmd == "/pause":
            self.db.set("paused", "1")
            self.say("⏸ Автопубликация на паузе. Ролики продолжат собираться на одобрение.")
        elif cmd == "/resume":
            self.db.set("paused", "0")
            self.say("▶️ Автопубликация включена.")
        elif cmd == "/ai":
            self.say("Проверяю нейросети…")
            self.run_bg(lambda: self.say(self.s.check_ai()))
        elif cmd == "/models":
            self.say("Запрашиваю списки моделей у посредников…")
            self.run_bg(lambda: self.say(self.s.models_report()))
        elif cmd == "/trends":
            self.say("Смотрю свежие тренды Reels и Shorts, это займёт около минуты…")
            self.run_bg(lambda: self.say(self.s.scout_trends()))
        elif cmd == "/move":
            self.move_last(args)
        elif cmd == "/scan":
            added = images.scan(self.cfg, self.db)
            self.say(f"Новых файлов: {sum(added.values())}. Разметка библиотеки идёт в фоне.")
        else:
            self.say("Не знаю такой команды.\n\n" + HELP)

    def queue_text(self) -> str:
        lines = []
        for status, label in (("pending", "Ждут одобрения"), ("approved", "Одобрены, ждут публикации")):
            rows = self.db.posts(status)
            lines.append(f"{label}: {len(rows)}")
            lines += [f"  #{r['id']} · {FORMAT_RU.get(r['format'], r['format'])} · {r['theme']}" for r in rows[:10]]
        return "\n".join(lines)

    def status_text(self) -> str:
        counts = {r["status"]: r["n"] for r in self.db.q("SELECT status, COUNT(*) n FROM posts GROUP BY status")}
        lib = self.db.one("SELECT COUNT(*) n, SUM(title IS NOT NULL) t FROM assets WHERE kind='library'")
        last = self.db.one("SELECT published_at, permalink FROM posts WHERE status='published' ORDER BY published_at DESC LIMIT 1")
        return "\n".join([
            f"Автопубликация: {'на паузе' if self.paused else 'включена'}",
            f"Instagram: {'подключён' if self.s.instagram_ready() else 'не подключён — публикуешь вручную'}",
            f"Источник фото: {self.cfg.image_source}",
            f"Библиотека: {lib['n'] or 0} фото, размечено {lib['t'] or 0}",
            f"Селфи «до»: {len(self.db.assets('before'))}, референсов: {len(self.db.assets('ref'))}",
            f"Нейросеть: {'⚠️ ' + self.db.get('last_ai_error') if self.db.get('last_ai_error') else 'отвечает'}"
            if self.db.get("last_ai_error") is not None else "Нейросеть: ещё не вызывалась",
            *([f"⚠️ Последняя ошибка разметки: {self.db.get('last_tag_error')}"]
              if self.db.get("last_tag_error") else []),
            "Посты: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "пока нет"),
            f"Последняя публикация: {last['published_at']} {last['permalink'] or ''}" if last else "Публикаций пока нет",
        ])

    # --------------------------------------------------------- callbacks
    def on_callback(self, cq: dict) -> None:
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        action, _, raw_id = data.partition(":")
        if action == "noop" or not raw_id.isdigit():
            self.api.answer_callback(cq["id"])
            return
        post_id = int(raw_id)
        mark = lambda label: self.api.edit_markup(self.admin, msg.get("message_id"), _status_keyboard(label))  # noqa: E731

        if action == "txt":
            post = self.db.post(post_id)
            self.api.answer_callback(cq["id"])
            if post:
                self.say(post["caption"] or "(пусто)")
            return

        if action in ("ok", "now"):
            if not self.db.claim_post(post_id, "pending", "approved"):
                self.api.answer_callback(cq["id"], "Уже обработан")
                return
            if not self.s.instagram_ready():
                self.db.update_post(post_id, status="manual")
                self.api.answer_callback(cq["id"], "Готово к ручной публикации")
                mark("📲 Публикуешь вручную")
                post = self.db.post(post_id)
                self.say("Instagram ещё не подключён. Сохрани видео выше и опубликуй как Reels, "
                         "добавь трендовый звук в приложении. Текст поста — следующим сообщением.")
                self.say(post["caption"] or "")
                return
            if action == "ok":
                self.api.answer_callback(cq["id"], "В очереди на публикацию")
                mark("✅ В расписании")
                return
            self._publish_asap(cq, post_id, mark)
            return

        if action == "retry":
            if not self.db.claim_post(post_id, "failed", "approved"):
                self.api.answer_callback(cq["id"], "Уже обработан")
                return
            self._publish_asap(cq, post_id, mark)
            return

        if action in ("no", "redo"):
            if not self.db.claim_post(post_id, "pending", "rejected"):
                self.api.answer_callback(cq["id"], "Уже обработан")
                return
            self.api.answer_callback(cq["id"], "Отклонён")
            mark("❌ Отклонён")
            if action == "redo":
                old = self.db.post(post_id)
                self.say("Собираю другой вариант…")

                def job():
                    new_id = self._make()
                    if new_id is None:
                        return
                    if old["scheduled_for"]:
                        self.db.update_post(new_id, scheduled_for=old["scheduled_for"])
                    self.send_for_approval(new_id)
                self.run_bg(job)
            return

        self.api.answer_callback(cq["id"])
