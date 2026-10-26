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

from . import brief, funnel, images, music, musicgen
from . import playbook as pbm
from .config import Config
from .db import DB
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger(__name__)

FORMAT_RU = {"before_after": "было → стало", "looks_carousel": "карусель образов", "worlds": "разные миры"}

HELP = """Команды:
/make — собрать ролик прямо сейчас
/make хэллоуин — ролик к поводу (или /make повод — к ближайшему)
/occasions — праздники и инфоповоды на 30 дней
/queue — что ждёт одобрения и публикации
/status — состояние системы
/ai — проверить ключи и модели нейросетей
/models — какие модели есть у посредников
/imgtest — сгенерировать пробный кадр по твоим фото
/brief — что система знает о продукте и аудитории (можно заменить своим)
/trends — обновить тренды прямо сейчас
/stats — статистика роликов
/youtube — подключить YouTube Shorts
/vk — подключить VK Клипы (ролики будут выходить и в сообщество VK)
/balance — балансы и лимиты всех платных сервисов
/funnel — сколько пришло в бот по ссылкам, сделали фото и купили (/funnel 7 — за неделю)
/links — ссылки на бот с меткой для шапки каждой соцсети
/analyze — пересчитать плейбук по статистике
/pause и /resume — остановить или включить автопубликацию
/scan — разобрать новые фото
/photos — хранилище фото: посмотреть, удалить, перенести
/music — какая музыка есть (добавить — прислать mp3 с подписью «музыка мистика»)
/musictest — создать пробный трек нейросетью
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
    test_image: Callable[[], tuple] | None = None   # -> (путь к кадру, путь генерации, секунды)
    make_for: Callable[[str], int] | None = None     # ролик к поводу по названию
    occasions_text: Callable[[], str] = lambda: "Календарь поводов недоступен"
    on_approved: Callable[[int, bool], str] | None = None   # поставить публикации на других площадках
    vk_connect: Callable[[str], str] | None = None          # сохранить вход в VK из адреса страницы
    vk_login: Callable[[], str] | None = None               # ссылка входа VK ID
    vk_retry: Callable[[int], str] | None = None
    balances_text: Callable[[], str] = lambda: "Проверка балансов недоступна"
    vk_status: Callable[[], str] = lambda: "VK: не подключён"
    yt_status: Callable[[], str] = lambda: "YouTube: не подключён"
    ig_status: Callable[[], str] | None = None
    yt_login: Callable[[], str] | None = None
    yt_retry: Callable[[int], str] | None = None


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
            f"аудитория: {post['audience']}" if post["audience"] else "",
            "эксперимент" if post["explore"] else "",
        ) if x)
        when = f"\nПубликация: {post['scheduled_for']}" if post["scheduled_for"] else ""
        warn = ""
        if plan.get("llm_ok") is False:
            warn = (f"⚠️ Нейросеть не ответила, тексты по запасному шаблону.\n"
                    f"Причина: {(plan.get('llm_error') or 'неизвестна')[:200]}\n\n")
        head = f"{warn}#{post_id} · {tags}{when}\n\nХук: {plan['script']['hook'].replace('*', '')}"
        text = post["caption"] or plan["post_caption"]
        caption = f"{head}\n\n{text}"
        # подпись к видео в Telegram — до 1024 символов; длинный текст поста шлём следом целиком
        follow = None
        if len(caption) > 1024:
            caption, follow = head[:1024], f"Текст поста #{post_id}:\n\n{text}"[:4096]
        try:
            msg = self.api.send_video(self.admin, post["video_path"], caption,
                                      approval_keyboard(post_id), cover=post["cover_path"])
        except Exception as e:  # noqa: BLE001 — вернём в очередь, досылка попробует снова
            log.error("send_video #%s: %s", post_id, e)
            self.db.update_post(post_id, status="rendered")
            return False
        self.db.update_post(post_id, status="pending", approval_message_id=msg["message_id"])
        if follow:
            try:
                self.api.send_message(self.admin, follow)
            except Exception as e:  # noqa: BLE001 — ролик уже на одобрении, текст не критичен
                log.warning("full caption #%s: %s", post_id, e)
        return True

    def send_vk_package(self, post_id: int, text: str) -> bool:
        """Файл ролика и текст для ручной публикации в VK Клипы."""
        post = self.db.post(post_id)
        if not post or not post["video_path"] or not Path(post["video_path"]).exists():
            self.say(f"⚠️ VK: файл ролика #{post_id} уже удалён — выложить не получится.")
            return True
        try:
            self.api.send_document(self.admin, post["video_path"],
                                   f"📘 Пора в VK Клипы: ролик #{post_id}. Сохрани файл и выложи клип "
                                   "в сообществе (в VK-редакторе можно заменить звук на трек из VK Музыки). "
                                   "Текст — следующим сообщением.",
                                   {"inline_keyboard": [[{"text": "✅ Выложил в VK", "callback_data": f"vkdone:{post_id}"}]]})
        except Exception as e:  # noqa: BLE001
            log.warning("vk package #%s: %s", post_id, e)
            return False
        self.say(text)
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
        cap = (msg.get("caption") or "").strip().lower()
        if msg.get("audio") or (msg.get("document") and cap.startswith("музык")
                                and Path((msg["document"].get("file_name") or "")).suffix.lower() in music.AUDIO_EXT):
            self.on_music(msg)
            return
        if msg.get("photo") or msg.get("document"):
            self.on_file(msg)
            return
        text = (msg.get("text") or "").strip()
        if "access_token=" in text and "oauth.vk." in text:
            # в сообщении ключ доступа — убираем его из переписки сразу
            self.api.delete_message(chat_id, msg.get("message_id"))
            if not self.s.vk_connect:
                self.say("Подключение VK недоступно.")
                return
            try:
                self.say(self.s.vk_connect(text))
            except Exception as e:  # noqa: BLE001
                self.say(f"⚠️ VK не подключился: {e}")
            return
        if "oauth.vk." in text and "/authorize" in text:
            self.say("Это ссылка для входа, её нужно открыть в браузере. После того как разрешишь доступ, "
                     "VK покажет почти пустую страницу — пришли адрес именно этой страницы "
                     "(в нём будет access_token=…).")
            return
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
            if ext in (".md", ".txt") and (msg.get("caption") or "").strip().lower().startswith("бриф"):
                self.on_brief_file(doc)
                return
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

    def _image_test(self) -> None:
        if not self.s.test_image:
            self.say("Проверка генерации недоступна.")
            return
        try:
            path, route, secs = self.s.test_image()
        except Exception as e:  # noqa: BLE001
            self.say(f"⚠️ Кадр не получился.\n{str(e)[:1500]}")
            return
        try:
            self.api.send_photo(self.admin, path, f"✅ Генерация работает: {route}, {secs:.0f} с.\n"
                                                  "Похож? Если сходство слабое — добавь референсов «реф» "
                                                  "с разных ракурсов при хорошем свете.")
        finally:
            Path(path).unlink(missing_ok=True)

    # -------------------------------------------------------------- music
    def _music_test(self) -> None:
        import random
        pb = pbm.load(self.cfg.playbook_path)
        style, folder = musicgen.choose_style(pb, None, random.Random())
        try:
            path = musicgen.generate(self.cfg, self.db, style, folder, 15)
        except musicgen.MusicError as e:
            self.say(f"⚠️ Трек не получился: {e}")
            return
        self.api.send_audio(self.admin, path, f"✅ Музыка работает. Стиль: {style}\nТрек сохранён в фонотеку.")

    def on_music(self, msg: dict) -> None:
        f = msg.get("audio") or msg.get("document")
        name = (f.get("file_name") or "track.mp3").lower()
        ext = Path(name).suffix.lower()
        if ext not in music.AUDIO_EXT:
            ext = ".mp3"
        if (f.get("file_size") or 0) > 20 * 1024 * 1024:
            self.say("Трек больше 20 МБ — Telegram не даст его скачать боту.")
            return
        mood = music.mood_from_caption(msg.get("caption") or "")
        dest = music.music_dir(self.cfg) / mood / f"{uuid.uuid4().hex[:10]}{ext}"
        self.api.download(f["file_id"], dest)
        self.say(f"Сохранил трек, настроение: {music.MOOD_RU[mood]}.\n\n{music.summary(self.cfg)}")

    # -------------------------------------------------------------- brief
    def on_brief_file(self, doc: dict) -> None:
        if (doc.get("file_size") or 0) > 200_000:
            self.say("Файл брифа слишком большой. Нужен обычный текст до 12 000 символов.")
            return
        tmp = self.cfg.data_dir / "work" / f"brief_{uuid.uuid4().hex[:8]}.txt"
        try:
            self.api.download(doc["file_id"], tmp)
            text = tmp.read_text(encoding="utf-8", errors="replace")
            brief.save(self.cfg, text)
        except ValueError as e:
            self.say(f"Бриф не сохранил: {e}")
            return
        finally:
            tmp.unlink(missing_ok=True)
        self.say(f"Бриф обновлён ({len(text)} символов). Сценарист и разведчик используют его со следующего ролика.")

    def send_brief_proposals(self) -> int:
        try:
            props = json.loads(self.db.get("brief_proposals") or "[]")
        except ValueError:
            props = []
        sent = 0
        for i, text in enumerate(props):
            if not text:
                continue
            sent += 1
            self.say(f"Предлагаю дописать в бриф:\n«{text}»",
                     {"inline_keyboard": [[{"text": "✅ Добавить", "callback_data": f"bok:{i}"},
                                           {"text": "❌ Не нужно", "callback_data": f"bno:{i}"}]]})
        return sent

    def on_brief_callback(self, cq: dict, data: str) -> None:
        if cq["from"]["id"] != self.admin:
            self.api.answer_callback(cq["id"])
            return
        action, _, raw = data.partition(":")
        msg_id = (cq.get("message") or {}).get("message_id")
        try:
            props = json.loads(self.db.get("brief_proposals") or "[]")
            text = props[int(raw)]
        except (ValueError, IndexError):
            self.api.answer_callback(cq["id"], "Предложение устарело")
            return
        if not text:
            self.api.answer_callback(cq["id"], "Уже обработано")
            return
        props[int(raw)] = ""  # повторное нажатие ничего не сделает
        self.db.set("brief_proposals", json.dumps(props, ensure_ascii=False))
        if action == "bok":
            try:
                brief.append_note(self.cfg, text)
            except ValueError as e:
                self.api.answer_callback(cq["id"])
                self.say(f"Не добавил: {e}")
                return
            self.api.answer_callback(cq["id"], "Добавлено в бриф")
            self.api.edit_markup(self.admin, msg_id, _status_keyboard("✅ В брифе"))
        else:
            self.api.answer_callback(cq["id"], "Пропущено")
            self.api.edit_markup(self.admin, msg_id, _status_keyboard("❌ Пропущено"))

    def send_brief(self) -> None:
        text = brief.load(self.cfg)
        head = ("Сейчас используется твой бриф." if brief.is_custom(self.cfg)
                else "Сейчас используется стандартный бриф.")
        self.say(head + " Чтобы заменить — пришли файл .md или .txt с подписью «бриф». "
                 "Вернуть стандартный — /brief_reset.")
        for i in range(0, len(text), 3800):
            self.say(text[i:i + 3800])

    # ------------------------------------------------------ photo manager
    PHOTO_KINDS = {"library": "Библиотека", "before": "Селфи «до»", "ref": "Референсы",
                   "generated": "Сгенерированные"}
    KIND_DIR = {"library": "library", "before": "before", "ref": "refs"}
    PAGE = 10

    def photos_menu(self) -> None:
        rows = [[{"text": f"{label} ({len(self.db.assets(kind))})", "callback_data": f"pf:{kind}:0"}]
                for kind, label in self.PHOTO_KINDS.items()]
        self.say("Хранилище фото. Что где используется:\n"
                 "• Библиотека — готовые кадры, попадают в ролики.\n"
                 "• Селфи «до» — показываются в роликах «было → стало» и служат образцом лица.\n"
                 "• Референсы — только образец лица для генерации, в ролики не попадают никогда.\n"
                 "• Сгенерированные — новые кадры от системы, тоже идут в ролики.\n\n"
                 "Открой папку, выбери фото по номеру — его можно удалить или перенести.",
                 {"inline_keyboard": rows})

    def _thumb_file(self, path: str, i: int) -> Path:
        out = self.cfg.data_dir / "work" / f"thumb_{uuid.uuid4().hex[:6]}_{i}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(images._thumb(path, 640))
        return out

    def show_photos(self, kind: str, offset: int) -> None:
        rows = self.db.q("SELECT * FROM assets WHERE kind=? ORDER BY id LIMIT ? OFFSET ?",
                         (kind, self.PAGE, offset))
        total = len(self.db.assets(kind))
        if not rows:
            self.say(f"В папке «{self.PHOTO_KINDS.get(kind, kind)}» пусто.")
            return
        thumbs, captions, ok_rows = [], [], []
        try:
            for i, r in enumerate(rows):
                try:
                    thumbs.append(self._thumb_file(r["path"], i))
                except OSError:
                    continue  # файл не читается — без кнопки, чтобы номера не съехали
                ok_rows.append((offset + i + 1, r))
                captions.append(f"{offset + i + 1}. {r['title'] or ''}".strip())
            try:
                if len(thumbs) == 1:  # альбом в Telegram — от 2 фото
                    self.api.send_photo(self.admin, thumbs[0], captions[0])
                elif thumbs:
                    self.api.send_media_group(self.admin, thumbs, captions)
            except TelegramError as e:
                log.warning("album: %s", e)
                self.say("Миниатюры не отправились, но кнопки ниже работают.")
        finally:
            for t in thumbs:
                t.unlink(missing_ok=True)
        broken = len(rows) - len(ok_rows)
        buttons = [{"text": str(num), "callback_data": f"pa:{r['id']}"} for num, r in ok_rows]
        keyboard = [buttons[:5], buttons[5:]] if len(buttons) > 5 else [buttons]
        if offset + self.PAGE < total:
            keyboard.append([{"text": "Дальше ▶", "callback_data": f"pf:{kind}:{offset + self.PAGE}"}])
        note = f" Не читаются файлов: {broken} — их лучше удалить и загрузить заново." if broken else ""
        self.say(f"{self.PHOTO_KINDS.get(kind, kind)}: {offset + 1}–{offset + len(rows)} из {total}. "
                 f"Выбери номер фото:{note}", {"inline_keyboard": [k for k in keyboard if k]})

    def photo_actions(self, asset_id: int) -> None:
        row = self.db.one("SELECT * FROM assets WHERE id=?", (asset_id,))
        if not row:
            self.say("Этого фото уже нет.")
            return
        moves = [{"text": f"→ {label}", "callback_data": f"pm:{asset_id}:{kind}"}
                 for kind, label in self.PHOTO_KINDS.items() if kind in self.KIND_DIR and kind != row["kind"]]
        self.say(f"Фото «{row['title'] or 'без названия'}» — сейчас в папке «{self.PHOTO_KINDS.get(row['kind'])}», "
                 f"использовалось в роликах: {row['used_count']}.",
                 {"inline_keyboard": [[{"text": "🗑 Удалить", "callback_data": f"pd:{asset_id}"}], moves]})

    def delete_photo(self, asset_id: int) -> bool:
        """Фото уходит в корзину на томе (DATA_DIR/trash), а не стирается насовсем."""
        row = self.db.one("SELECT * FROM assets WHERE id=?", (asset_id,))
        if not row:
            return False
        src = Path(row["path"])
        if src.exists():
            trash = self.cfg.data_dir / "trash"
            trash.mkdir(parents=True, exist_ok=True)
            src.replace(trash / f"{row['id']}_{src.name}")
        self.db.x("DELETE FROM assets WHERE id=?", (asset_id,))
        return True

    def on_photo_callback(self, cq: dict, data: str) -> None:
        if cq["from"]["id"] != self.admin:
            self.api.answer_callback(cq["id"])
            return
        parts = data.split(":")
        self.api.answer_callback(cq["id"])
        try:
            if parts[0] == "pf":
                self.run_bg(lambda: self.show_photos(parts[1], int(parts[2])))
            elif parts[0] == "pa":
                self.photo_actions(int(parts[1]))
            elif parts[0] == "pd":
                ok = self.delete_photo(int(parts[1]))
                self.say(("Удалил (лежит в корзине на томе). " if ok else "Этого фото уже нет. ")
                         + self._counts_text())
            elif parts[0] == "pm":
                row = self.db.one("SELECT * FROM assets WHERE id=?", (int(parts[1]),))
                target = self.KIND_DIR.get(parts[2])
                if not row or not target:
                    self.say("Этого фото уже нет.")
                    return
                self.move_asset(row, target)
                self.say("Перенёс. " + self._counts_text())
        except (ValueError, IndexError):
            self.say("Не понял кнопку — открой /photos заново.")

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
                f"референсы: {len(self.db.assets('ref'))}, сгенерированные: {len(self.db.assets('generated'))}.")

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
        elif cmd == "/make" and args and self.s.make_for:
            query = " ".join(args)
            self.say(f"Собираю ролик к поводу «{query}»: генерирую новые кадры, это 3–6 минут…")

            def job():
                try:
                    post_id = self.s.make_for(query)
                except Exception as e:  # noqa: BLE001
                    self.say(f"⚠️ Не получилось: {e}")
                    return
                if not self.send_for_approval(post_id):
                    self.say(f"Ролик #{post_id} собран, отправлю его чуть позже.")
            self.run_bg(job)
        elif cmd == "/occasions":
            self.say(self.s.occasions_text())
        elif cmd == "/make":
            self.say("Собираю ролик, это займёт 1–3 минуты (с генерацией новых кадров — до 6)…")

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
        elif cmd == "/imgtest":
            self.say("Генерирую пробный кадр по твоим фото, это 20–90 секунд…")
            self.run_bg(self._image_test)
        elif cmd == "/trends":
            self.say("Смотрю свежие тренды Reels и Shorts, это займёт около минуты…")
            self.run_bg(lambda: self.say(self.s.scout_trends()))
        elif cmd == "/brief":
            self.send_brief()
        elif cmd == "/brief_reset":
            brief.reset(self.cfg)
            self.say("Вернул стандартный бриф.")
        elif cmd == "/music":
            self.say(music.summary(self.cfg) + "\n\n" + musicgen.status(self.cfg, self.db))
        elif cmd == "/musictest":
            if not musicgen.configured(self.cfg):
                self.say(musicgen.status(self.cfg, self.db))
            else:
                self.say("Создаю трек в трендовом стиле, это до пары минут…")
                self.run_bg(self._music_test)
        elif cmd == "/vk":
            self.say(self.vk_help())
        elif cmd in ("/youtube", "/yt"):
            self.say(self.yt_help())
        elif cmd == "/balance":
            self.say("Проверяю балансы сервисов…")
            self.run_bg(lambda: self.say(self.s.balances_text()))
        elif cmd == "/funnel":
            days = next((int(a) for a in args if a.isdigit()), 30)
            self.say("Считаю переходы в бот…")
            self.run_bg(lambda: self.say(funnel.report(self.cfg, days)))
        elif cmd == "/links":
            self.say(funnel.links_text(self.cfg))
        elif cmd == "/photos":
            self.photos_menu()
        elif cmd == "/move":
            self.move_last(args)
        elif cmd == "/scan":
            added = images.scan(self.cfg, self.db)
            self.say(f"Новых файлов: {sum(added.values())}. Разметка библиотеки идёт в фоне.")
        else:
            self.say("Не знаю такой команды.\n\n" + HELP)

    def yt_help(self) -> str:
        from urllib.parse import urlparse
        host = urlparse(self.cfg.public_base_url).netloc or "адрес-движка.up.railway.app"
        setup = ("Один раз в Google Cloud (console.cloud.google.com, твой проект):\n"
                 "1. «APIs & Services → Library» → YouTube Data API v3 → Enable.\n"
                 "2. «OAuth consent screen» (Google Auth Platform → Branding/Audience): тип External, "
                 "заполни название и почту, затем Audience → «Publish app» (статус In production). "
                 "Иначе Google отзывает вход через 7 дней.\n"
                 "3. «Credentials → Create credentials → OAuth client ID», тип «Web application», "
                 f"Authorized redirect URI: https://{host}/yt/callback\n"
                 "4. Client ID и Client secret — в Railway: YT_CLIENT_ID и YT_CLIENT_SECRET, затем Deploy и снова /youtube.\n"
                 "После первой публикации я проверю, с какой видимостью YouTube принял ролик: если он окажется "
                 "закрытым (так YouTube иногда поступает с непроверенными проектами), подскажу, что делать.")
        if not (self.cfg.yt_client_id and self.cfg.yt_client_secret):
            return "Чтобы ролики выходили в YouTube Shorts:\n" + setup
        if not self.s.yt_login:
            return "Подключение YouTube недоступно."
        try:
            link = self.s.yt_login()
        except Exception as e:  # noqa: BLE001
            return f"⚠️ {e}"
        return ("Вход в YouTube:\n"
                f"1. Открой ссылку (одноразовая): {link}\n"
                "2. Выбери аккаунт с каналом. Google предупредит «приложение не проверено» — это твоё же "
                "приложение: «Дополнительно → Перейти…», отметь все галочки и разреши.\n"
                "3. Откроется страница «✅ YouTube подключён», а сюда придёт отчёт.\n\n"
                "Если Google пишет redirect_uri_mismatch — проверь адрес возврата:\n" + setup)

    def vk_help(self) -> str:
        from urllib.parse import urlparse
        host = urlparse(self.cfg.public_base_url).netloc or "адрес-движка.up.railway.app"
        setup = ("Настройки приложения в кабинете VK ID (id.vk.ru/about/business/go → Мои приложения):\n"
                 "• платформа «Web»;\n"
                 f"• базовый домен: {host}\n"
                 f"• доверенный redirect URL: https://{host}/vk/callback\n"
                 "• Доступы: включи «Стена», «Видео» и «Сообщества» (это расширенные доступы — "
                 "нужен подтверждённый профиль бизнеса, ИП подходит).")
        if not self.cfg.vk_app_id:
            return ("Чтобы ролики выходили в VK Клипы:\n" + setup +
                    "\n\nЗатем в Railway: VK_APP_ID = ID приложения, VK_GROUP_ID = номер сообщества "
                    "(цифры из адреса club123456). После деплоя снова /vk — пришлю ссылку входа.")
        if not self.s.vk_login:
            return "Подключение VK недоступно."
        try:
            link = self.s.vk_login()
        except Exception as e:  # noqa: BLE001
            return f"⚠️ {e}"
        return ("Вход в VK:\n"
                f"1. Открой ссылку (действует один раз): {link}\n"
                "2. Войди и разреши доступы. VK вернёт тебя на страницу движка «✅ VK подключён», "
                "а сюда придёт отчёт. Копировать ничего не нужно.\n\n"
                "Если VK пишет ошибку про redirect_uri или scope — проверь настройки:\n" + setup)

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
            self.s.ig_status() if self.s.ig_status else
            f"Instagram: {'подключён' if self.s.instagram_ready() else 'не подключён — публикуешь вручную'}",
            self.s.vk_status(),
            self.s.yt_status(),
            f"Тексты роликов пишет: {self.cfg.copy_model or 'основная модель'}"
            + (f" (⚠️ {self.db.get('last_copy_error')[:150]})" if self.db.get("last_copy_error") else ""),
            f"Источник фото: {self.cfg.image_source}; сгенерировано сегодня: "
            f"{images.images_used_today(self.db)} из {self.cfg.max_images_per_day}",
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
        if data.split(":", 1)[0] in ("pf", "pa", "pd", "pm"):
            self.on_photo_callback(cq, data)
            return
        if data.split(":", 1)[0] in ("bok", "bno"):
            self.on_brief_callback(cq, data)
            return
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

        if action == "vkdone":
            if self.db.claim_crosspost(post_id, "vk_manual", "sent", "published"):
                self.api.answer_callback(cq["id"], "Отмечено")
                mark("✅ Выложено в VK")
            else:
                self.api.answer_callback(cq["id"], "Уже отмечено")
            return

        if action in ("vkretry", "ytretry"):
            self.api.answer_callback(cq["id"])
            fn = self.s.vk_retry if action == "vkretry" else self.s.yt_retry
            if fn:
                self.say(fn(post_id))
            return

        if action in ("ok", "now"):
            if not self.db.claim_post(post_id, "pending", "approved"):
                self.api.answer_callback(cq["id"], "Уже обработан")
                return
            other = ""
            if self.s.on_approved:
                try:
                    other = self.s.on_approved(post_id, action == "now")
                except Exception as e:  # noqa: BLE001 — Instagram важнее, сообщим и продолжим
                    log.exception("on_approved #%s", post_id)
                    other = f"⚠️ Другие площадки: {e}"
            if other:
                self.say(other)
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
