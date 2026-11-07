"""Минимальный клиент Telegram Bot API на requests."""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import requests
from PIL import Image

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


def _thumbnail(path: Path) -> bytes:
    """Telegram принимает миниатюру не больше 320 px по стороне и до 200 КБ."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        im.thumbnail((320, 320))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
    return buf.getvalue()


class TelegramAPI:
    def __init__(self, token: str, base: str = "https://api.telegram.org"):
        self.token = token
        self.base = base.rstrip("/")

    def _url(self, method: str) -> str:
        return f"{self.base}/bot{self.token}/{method}"

    def call(self, method: str, http_timeout: int = 60, files: dict | None = None, **params):
        data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                for k, v in params.items() if v is not None}
        try:
            r = requests.post(self._url(method), data=data, files=files, timeout=http_timeout)
            payload = r.json()
        except (requests.RequestException, ValueError) as e:
            # В тексте ошибки requests бывает URL с токеном — вырезаем его.
            raise TelegramError(f"{method}: {str(e).replace(self.token, '***')}") from None
        if not payload.get("ok"):
            raise TelegramError(f"{method}: {payload.get('description')}")
        return payload["result"]

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        return self.call("getUpdates", http_timeout=timeout + 10, offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None,
                     reply_to: int | None = None) -> dict:
        return self.call("sendMessage", chat_id=chat_id, text=text[:4096],
                         reply_markup=reply_markup, disable_web_page_preview="true",
                         reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                         if reply_to else None)

    def copy_message(self, chat_id: int, from_chat_id: int, message_id: int,
                     reply_markup: dict | None = None) -> dict:
        """Копия сообщения (текст, голосовое, фото, файл) без подписи «переслано от»."""
        return self.call("copyMessage", chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id,
                         reply_markup=reply_markup)

    def send_video(self, chat_id: int, path: str | Path, caption: str = "",
                   reply_markup: dict | None = None, cover: str | Path | None = None) -> dict:
        files = {"video": (Path(path).name, Path(path).read_bytes(), "video/mp4")}
        if cover and Path(cover).exists():
            files["thumbnail"] = ("thumb.jpg", _thumbnail(Path(cover)), "image/jpeg")
        return self.call("sendVideo", http_timeout=180, files=files, chat_id=chat_id, caption=caption[:1024],
                         reply_markup=reply_markup, supports_streaming="true",
                         width=1080, height=1920)

    def send_photo(self, chat_id: int, path: str | Path, caption: str = "",
                   reply_markup: dict | None = None) -> dict:
        files = {"photo": (Path(path).name, Path(path).read_bytes(), "image/jpeg")}
        return self.call("sendPhoto", http_timeout=120, files=files, chat_id=chat_id,
                         caption=caption[:1024], reply_markup=reply_markup)

    def send_document(self, chat_id: int, path: str | Path, caption: str = "",
                      reply_markup: dict | None = None) -> dict:
        """Файл без пережатия — для загрузки в другие соцсети в исходном качестве."""
        mime = {".pdf": "application/pdf", ".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".docx":
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}.get(
            Path(path).suffix.lower(), "application/octet-stream")
        files = {"document": (Path(path).name, Path(path).read_bytes(), mime)}
        return self.call("sendDocument", http_timeout=180, files=files, chat_id=chat_id,
                         caption=caption[:1024], reply_markup=reply_markup)

    def send_audio(self, chat_id: int, path: str | Path, caption: str = "") -> dict:
        files = {"audio": (Path(path).name, Path(path).read_bytes(), "audio/mpeg")}
        return self.call("sendAudio", http_timeout=120, files=files, chat_id=chat_id, caption=caption[:1024])

    def send_media_group(self, chat_id: int, paths: list[str | Path], captions: list[str] | str | None = None,
                         as_documents: bool = False) -> list:
        """До 10 фото (или файлов) одним альбомом. captions — строка (подпись к первому) или список подписей."""
        if isinstance(captions, str):
            captions = [captions]
        kind = "document" if as_documents else "photo"
        files, media = {}, []
        for i, p in enumerate(paths[:10]):
            key = f"p{i}"
            files[key] = (Path(p).name, Path(p).read_bytes(), "image/jpeg")
            item = {"type": kind, "media": f"attach://{key}"}
            if captions and i < len(captions) and captions[i]:
                item["caption"] = captions[i][:1024]
            media.append(item)
        return self.call("sendMediaGroup", http_timeout=180, files=files, chat_id=chat_id, media=media)

    def edit_markup(self, chat_id: int, message_id: int, reply_markup: dict | None) -> None:
        try:
            self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                      reply_markup=reply_markup or {"inline_keyboard": []})
        except TelegramError as e:  # «message is not modified» и т.п. — не критично
            log.debug("edit_markup: %s", e)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TelegramError as e:
            log.debug("delete_message: %s", e)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])
        except TelegramError as e:
            log.debug("answer_callback: %s", e)

    def download(self, file_id: str, dest: Path) -> Path:
        info = self.call("getFile", file_id=file_id)
        url = f"{self.base}/file/bot{self.token}/{info['file_path']}"
        try:
            r = requests.get(url, timeout=120)
        except requests.RequestException as e:
            raise TelegramError(f"download: {str(e).replace(self.token, '***')}") from None
        if r.status_code != 200:
            raise TelegramError(f"download {r.status_code}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
