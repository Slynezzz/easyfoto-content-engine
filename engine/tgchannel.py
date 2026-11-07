"""Публикация роликов в Telegram-канал.

Админ-бот движка добавляется администратором канала (право «Публикация
сообщений»), номер или @имя канала — в TG_CHANNEL_ID. Ролик уходит в канал
в тот же слот, что и на другие площадки: видео с подписью, а если текст поста
длиннее лимита подписи Telegram (1024 символа) — видео с началом текста и
следом остальное. Ссылки в Telegram кликабельны, поэтому призыв ведёт прямо
по ссылке CTA_URL (или на бот с меткой utm_tg_post_p<id>).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import funnel, niche
from .config import Config
from .db import DB
from .telegram import TelegramAPI, TelegramError

log = logging.getLogger(__name__)
CAPTION_LIMIT = 1024


class TGChannelError(RuntimeError):
    pass


class TGChannel:
    def __init__(self, cfg: Config, db: DB, api: TelegramAPI | None):
        self.cfg, self.db, self.api = cfg, db, api

    def ready(self) -> bool:
        return bool(self.cfg.tg_channel_id and self.api)

    def text_for(self, post) -> str:
        caption = (post["caption"] or "").strip()
        link = funnel.cta_link(self.cfg, "tg", "post", f"p{post['id']}")
        label = niche.load(self.cfg.niche)["cta_label"] if self.cfg.lead_bot_token else ""
        return f"{caption}\n\n👉 {label + ': ' if label else ''}{link}"

    def _split(self, text: str) -> tuple[str, str]:
        if len(text) <= CAPTION_LIMIT:
            return text, ""
        cut = text.rfind("\n\n", 0, CAPTION_LIMIT)
        if cut < CAPTION_LIMIT // 2:
            cut = text.rfind(" ", 0, CAPTION_LIMIT)
        return text[:cut].rstrip(), text[cut:].strip()

    def _url(self, message_id: int) -> str:
        ch = str(self.cfg.tg_channel_id)
        if ch.startswith("@"):
            return f"https://t.me/{ch[1:]}/{message_id}"
        if ch.startswith("-100"):
            return f"https://t.me/c/{ch[4:]}/{message_id}"
        return f"сообщение {message_id}"

    def publish(self, post_id: int) -> str:
        post = self.db.post(post_id)
        if not post:
            raise TGChannelError(f"пост #{post_id} не найден")
        row = self.db.crosspost(post_id, "tg")
        if row and row["external_id"]:  # видео уже в канале — не дублируем
            return row["url"] or self._url(int(row["external_id"]))
        is_post = post["format"] in ("post", "carousel")
        media = Path((post["cover_path"] if is_post else post["video_path"]) or "")
        if not media.name or not media.exists():
            raise TGChannelError("файл " + ("картинки" if is_post else "ролика") + " уже удалён с тома")
        head, rest = self._split(self.text_for(post))
        try:
            if post["format"] == "carousel":
                slides = [media] + [Path(x) for x in json.loads(post["script_json"] or "{}").get("slides") or []]
                msgs = self.api.send_media_group(self.cfg.tg_channel_id, [x for x in slides if x.exists()], head)
                msg = msgs[0]
            elif is_post:
                msg = self.api.send_photo(self.cfg.tg_channel_id, media, head)
            else:
                msg = self.api.send_video(self.cfg.tg_channel_id, media, head, cover=post["cover_path"])
        except TelegramError as e:
            hint = (" — добавь админ-бота администратором канала с правом публикации и проверь TG_CHANNEL_ID"
                    if any(w in str(e).lower() for w in ("not enough rights", "chat not found", "forbidden",
                                                         "need administrator")) else "")
            raise TGChannelError(f"Telegram: {e}{hint}") from None
        mid = msg["message_id"]
        url = self._url(mid)
        self.db.update_crosspost(post_id, "tg", external_id=str(mid), url=url, error=None)
        if rest:
            try:
                self.api.send_message(self.cfg.tg_channel_id, rest)
            except TelegramError as e:  # видео уже в канале; остаток текста не критичен
                log.warning("tg channel tail #%s: %s", post_id, e)
        return url


