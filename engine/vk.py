"""Публикация роликов во ВКонтакте: видео в сообщество и пост на стене со ссылкой на бот.

Как входит в VK: владелец создаёт приложение VK (тип Standalone) и сообщает
его id в VK_APP_ID. Команда /vk даёт ссылку входа; после входа VK открывает
пустую страницу, адрес которой (с ключом доступа) владелец присылает
админ-боту. Ключ хранится в базе движка, сообщение с ним бот удаляет.

Что публикуется: video.save + загрузка файла в сообщество VK_GROUP_ID
(вертикальное короткое видео VK показывает в Клипах), затем пост на стене
сообщества с этим видео и кликабельной ссылкой на бот с меткой utm_vk_clip_p<id>.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

from . import funnel
from .config import Config
from .db import DB, now_iso

log = logging.getLogger(__name__)

API = "https://api.vk.com/method"
VERSION = "5.199"
BLANK = "https://oauth.vk.com/blank.html"
SCOPE = "video,wall,groups,offline"


class VKError(RuntimeError):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.code = code


HINTS = {
    5: "вход в VK устарел или отозван — пройди /vk заново",
    7: "у входа нет нужных доступов — пройди /vk заново и разреши видео и стену",
    15: "нет доступа: нужен аккаунт-администратор сообщества и доступы «видео» и «стена»",
    27: "этот метод не работает с ключом сообщества — нужен вход владельца через /vk",
    203: "нет доступа к сообществу: проверь VK_GROUP_ID и что ты в нём администратор",
    214: "публикация на стене запрещена настройками сообщества",
}


def auth_url(cfg: Config) -> str:
    return (f"https://oauth.vk.com/authorize?client_id={cfg.vk_app_id}&redirect_uri={BLANK}"
            f"&display=page&scope={SCOPE}&response_type=token&v={VERSION}")


def parse_token_url(text: str) -> tuple[str, int, str] | None:
    """Достаёт (ключ, срок в секундах, id пользователя) из адреса blank.html#access_token=…"""
    text = (text or "").strip()
    if "access_token=" not in text:
        return None
    frag = urlparse(text).fragment or text.split("#", 1)[-1]
    params = parse_qs(frag)
    token = (params.get("access_token") or [""])[0]
    if not token:
        return None
    try:
        expires = int((params.get("expires_in") or ["0"])[0])
    except ValueError:
        expires = 0
    return token, expires, (params.get("user_id") or [""])[0]


class VK:
    def __init__(self, cfg: Config, db: DB, sleep=time.sleep):
        self.cfg, self.db, self.sleep = cfg, db, sleep

    # ------------------------------------------------------------ ключ
    @property
    def token(self) -> str:
        return self.cfg.vk_access_token or self.db.get("vk_token", "") or ""

    def ready(self) -> bool:
        return bool(self.token and self.cfg.vk_group_id)

    def expires_at(self) -> datetime | None:
        raw = "" if self.cfg.vk_access_token else (self.db.get("vk_token_expires") or "")
        return datetime.fromisoformat(raw) if raw else None

    def save_token(self, text: str) -> str:
        """Принимает адрес после входа. Проверяет ключ и сообщество. Возвращает отчёт для владельца."""
        parsed = parse_token_url(text)
        if not parsed:
            raise VKError("в сообщении нет access_token — пришли адрес страницы целиком")
        token, expires, user_id = parsed
        user = self._call("users.get", token=token)
        name = " ".join(x for x in ((user or [{}])[0].get("first_name"), (user or [{}])[0].get("last_name")) if x)
        self.db.set("vk_token", token)
        self.db.set("vk_user_id", user_id)
        until = (datetime.now(timezone.utc) + timedelta(seconds=expires)).isoformat(timespec="seconds") \
            if expires else ""
        self.db.set("vk_token_expires", until)
        lines = [f"✅ VK подключён: {name or 'аккаунт ' + user_id}."]
        lines.append("Вход бессрочный." if not expires else
                     f"⚠️ VK выдал вход на {expires // 3600} ч — напомню заранее, когда понадобится войти снова.")
        if self.cfg.vk_group_id:
            try:
                g = self._call("groups.getById", group_id=self.cfg.vk_group_id)
                groups = g.get("groups", g) if isinstance(g, dict) else g
                gname = (groups or [{}])[0].get("name", "")
                admin = (groups or [{}])[0].get("is_admin")
                lines.append(f"Сообщество: {gname}" + ("" if admin in (None, 1) else
                                                       " — ⚠️ ты в нём не администратор, публикация не пройдёт"))
            except VKError as e:
                lines.append(f"⚠️ Сообщество {self.cfg.vk_group_id}: {e}")
        else:
            lines.append("Осталось указать сообщество: VK_GROUP_ID в Railway (число из адреса club123456).")
        return "\n".join(lines)

    # ------------------------------------------------------------ API
    def _call(self, method: str, token: str | None = None, **params):
        body = {**params, "access_token": token or self.token, "v": VERSION}
        for attempt in range(3):
            try:
                r = requests.post(f"{API}/{method}", data=body, timeout=60)
                data = r.json()
            except (requests.RequestException, ValueError) as e:
                if attempt == 2:
                    raise VKError(f"{method}: VK недоступен ({type(e).__name__})") from None
                self.sleep(3)
                continue
            err = data.get("error")
            if err:
                code = int(err.get("error_code") or 0)
                if code == 6 and attempt < 2:  # слишком много запросов в секунду
                    self.sleep(1.5)
                    continue
                msg = HINTS.get(code, err.get("error_msg") or "ошибка VK")
                raise VKError(f"{method}: {msg} (код {code})", code)
            return data.get("response")
        raise VKError(f"{method}: VK не ответил")

    def _upload(self, url: str, path: Path) -> dict:
        try:
            with open(path, "rb") as f:
                r = requests.post(url, files={"video_file": (path.name, f, "video/mp4")}, timeout=600)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise VKError(f"загрузка видео в VK не удалась ({type(e).__name__})") from None
        if "error" in data:
            raise VKError(f"загрузка видео в VK: {data.get('error')}")
        return data

    # ------------------------------------------------------------ публикация
    def text_for(self, post) -> tuple[str, str]:
        """(название видео, текст поста со ссылкой на бот)."""
        import json
        plan = json.loads(post["script_json"] or "{}")
        hook = str(plan.get("script", {}).get("hook") or "").replace("*", "").strip()
        link = funnel.link(self.cfg, "vk", "clip", f"p{post['id']}", target=self.cfg.vk_link_target)
        caption = (post["caption"] or "").strip()
        text = f"{caption}\n\n👉 Попробовать бесплатно: {link}"
        return (hook or "Нейрофото из обычных селфи")[:120], text[:4000]

    def publish(self, post_id: int) -> str:
        """Публикует ролик. Возвращает ссылку. Повтор после частичного успеха не дублирует видео."""
        post = self.db.post(post_id)
        if not post:
            raise VKError(f"пост #{post_id} не найден")
        video = Path(post["video_path"] or "")
        if not post["video_path"] or not video.exists():
            raise VKError("файл ролика уже удалён с тома")
        name, text = self.text_for(post)
        gid = int(self.cfg.vk_group_id)
        row = self.db.crosspost(post_id, "vk")
        ext = row["external_id"] if row else None
        if not ext:
            saved = self._call("video.save", name=name, description=text, group_id=gid,
                               wallpost=0, repeat=1, no_comments=0)
            up = self._upload(saved["upload_url"], video)
            owner = up.get("owner_id") or saved.get("owner_id") or -gid
            vid = up.get("video_id") or saved.get("video_id")
            ext = f"{owner}_{vid}"
            # сразу запоминаем: при сбое ниже видео не загрузится второй раз
            self.db.update_crosspost(post_id, "vk", external_id=ext, url=f"https://vk.com/video{ext}")
        url = f"https://vk.com/video{ext}"
        if self.cfg.vk_wallpost:
            res = self._call("wall.post", owner_id=-gid, from_group=1, message=text,
                             attachments=f"video{ext}")
            url = f"https://vk.com/wall-{gid}_{res.get('post_id')}"
        self.db.update_crosspost(post_id, "vk", url=url, error=None)
        return url

    def token_warning(self) -> str:
        """Предупреждение, если вход в VK скоро истечёт (за сутки)."""
        exp = self.expires_at()
        if not exp or not self.token:
            return ""
        left = exp - datetime.now(timezone.utc)
        if left <= timedelta(0):
            return "⚠️ Вход в VK истёк — ролики в VK не публикуются. Пройди /vk заново."
        if left <= timedelta(days=1):
            return f"⏳ Вход в VK истечёт через {max(1, int(left.total_seconds() // 3600))} ч. Пройди /vk заново."
        return ""

    def status(self) -> str:
        if not self.cfg.vk_app_id and not self.cfg.vk_access_token:
            return "VK: не подключён (нужны VK_APP_ID и VK_GROUP_ID, затем /vk)"
        if not self.token:
            return "VK: нужен вход — /vk"
        if not self.cfg.vk_group_id:
            return "VK: вход есть, но не указано сообщество VK_GROUP_ID"
        exp = self.expires_at()
        return f"VK: подключён, сообщество {self.cfg.vk_group_id}" + (f", вход до {exp:%d.%m %H:%M} UTC" if exp else "")
