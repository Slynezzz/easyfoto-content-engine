"""Публикация роликов во ВКонтакте: видео в сообщество и пост на стене со ссылкой на бот.

Как входит в VK. С июня 2024 года новые приложения VK получают ключи только через
VK ID (OAuth 2.1 с PKCE), старый «implicit flow» для них не работает (отсюда ошибка
invalid scope). Поэтому: приложение в кабинете VK ID (платформа Web) с доверенным
адресом возврата <PUBLIC_BASE_URL>/vk/callback и включёнными доступами «Стена»,
«Видео», «Сообщества». Команда /vk даёт ссылку входа, VK возвращает на наш сервер,
движок обменивает код на ключи и дальше сам продлевает их (ключ доступа живёт час,
ключ обновления — долго и обновляется при каждом продлении).

Для старых приложений оставлен и прежний способ: прислать боту адрес
blank.html#access_token=…, а также бессрочный ключ в VK_ACCESS_TOKEN.

Что публикуется: video.save + загрузка файла в сообщество VK_GROUP_ID
(вертикальное короткое видео VK показывает в Клипах), затем пост на стене
сообщества с этим видео и кликабельной ссылкой на бот с меткой utm_vk_clip_p<id>.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import funnel
from .config import Config
from .db import DB

log = logging.getLogger(__name__)

API = "https://api.vk.com/method"
VERSION = "5.199"
BLANK = "https://oauth.vk.com/blank.html"
LEGACY_SCOPE = "video,wall,groups,offline"
REFRESH_EARLY = timedelta(minutes=5)


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


def redirect_uri(cfg: Config) -> str:
    return f"{cfg.public_base_url}/vk/callback"


def legacy_auth_url(cfg: Config) -> str:
    return (f"https://oauth.vk.com/authorize?client_id={cfg.vk_app_id}&redirect_uri={BLANK}"
            f"&display=page&scope={LEGACY_SCOPE}&response_type=token&v={VERSION}")


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


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
        self._lock = threading.Lock()

    # ------------------------------------------------------------ ключ
    @property
    def token(self) -> str:
        """Действующий ключ доступа; при входе через VK ID продлевается сам."""
        if self.cfg.vk_access_token:
            return self.cfg.vk_access_token
        if self.db.get("vk_refresh_token"):
            with self._lock:
                exp = self._exp("vk_token_expires")
                if not self.db.get("vk_token") or (exp and exp - datetime.now(timezone.utc) < REFRESH_EARLY):
                    try:
                        self._refresh()
                    except VKError as e:
                        self.db.set("vk_auth_error", str(e)[:300])
                        log.warning("vk refresh: %s", e)
        return self.db.get("vk_token", "") or ""

    def ready(self) -> bool:
        has_key = self.cfg.vk_access_token or self.db.get("vk_token") or self.db.get("vk_refresh_token")
        return bool(has_key and self.cfg.vk_group_id)

    def can_autopublish(self) -> bool:
        """Есть вход с правами на видео и стену (новым приложениям VK их сейчас не выдаёт)."""
        if not self.ready():
            return False
        if self.cfg.vk_access_token:
            return True
        granted = self.db.get("vk_scope_granted") or ""
        return granted == "legacy" or {"video", "wall"} <= set(granted.split())

    def _exp(self, key: str) -> datetime | None:
        raw = self.db.get(key) or ""
        return datetime.fromisoformat(raw) if raw else None

    def expires_at(self) -> datetime | None:
        """Срок старого ключа без продления (при входе через VK ID не важен — продлеваем сами)."""
        if self.cfg.vk_access_token or self.db.get("vk_refresh_token"):
            return None
        return self._exp("vk_token_expires")

    # ------------------------------------------------------------ вход через VK ID
    def start_login(self) -> str:
        """Ссылка входа VK ID. Код возврата придёт на <PUBLIC_BASE_URL>/vk/callback."""
        if not self.cfg.public_base_url:
            raise VKError("не задан PUBLIC_BASE_URL — адрес сервиса движка в Railway")
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(32)
        self.db.set("vk_pkce_verifier", verifier)
        self.db.set("vk_pkce_state", state)
        q = {"response_type": "code", "client_id": self.cfg.vk_app_id, "redirect_uri": redirect_uri(self.cfg),
             "scope": self.cfg.vk_scope, "state": state, "code_challenge": challenge,
             "code_challenge_method": "S256"}
        return f"{self.cfg.vk_id_host}/authorize?{urlencode(q)}"

    def _oauth(self, data: dict) -> dict:
        if self.cfg.vk_client_secret:
            data["client_secret"] = self.cfg.vk_client_secret
        try:
            r = requests.post(f"{self.cfg.vk_id_host}/oauth2/auth", data=data, timeout=30)
            d = r.json()
        except (requests.RequestException, ValueError) as e:
            raise VKError(f"VK ID недоступен ({type(e).__name__})") from None
        if d.get("error") or not d.get("access_token"):
            raise VKError(f"VK ID: {d.get('error_description') or d.get('error') or 'нет ключа в ответе'}")
        return d

    def _store(self, d: dict, device_id: str) -> None:
        self.db.set("vk_token", d["access_token"])
        if d.get("refresh_token"):
            self.db.set("vk_refresh_token", d["refresh_token"])
        self.db.set("vk_device_id", device_id)
        exp = int(d.get("expires_in") or 3600)
        self.db.set("vk_token_expires", (datetime.now(timezone.utc) + timedelta(seconds=exp)).isoformat(timespec="seconds"))
        if d.get("user_id"):
            self.db.set("vk_user_id", str(d["user_id"]))
        self.db.set("vk_auth_error", "")

    def finish_login(self, params: dict) -> str:
        if params.get("error"):
            raise VKError(params.get("error_description") or params["error"])
        state = self.db.get("vk_pkce_state") or ""
        if not state or params.get("state") != state:
            raise VKError("ссылка входа устарела — набери /vk и войди по новой ссылке")
        if not params.get("code") or not params.get("device_id"):
            raise VKError("VK не вернул код входа")
        d = self._oauth({"grant_type": "authorization_code", "code": params["code"],
                         "code_verifier": self.db.get("vk_pkce_verifier") or "", "client_id": self.cfg.vk_app_id,
                         "device_id": params["device_id"], "redirect_uri": redirect_uri(self.cfg), "state": state})
        self.db.set("vk_pkce_state", "")  # ссылка одноразовая
        self._store(d, params["device_id"])
        self.db.set("vk_scope_granted", str(d.get("scope") or ""))
        return self._connected_report(d["access_token"], str(d.get("user_id") or ""), "VK ID",
                                      granted=str(d.get("scope") or ""))

    def _refresh(self) -> None:
        d = self._oauth({"grant_type": "refresh_token", "refresh_token": self.db.get("vk_refresh_token") or "",
                         "client_id": self.cfg.vk_app_id, "device_id": self.db.get("vk_device_id") or "",
                         "state": secrets.token_urlsafe(32)})
        self._store(d, self.db.get("vk_device_id") or "")

    # ------------------------------------------------------------ старый способ (адрес с ключом)
    def save_token(self, text: str) -> str:
        """Принимает адрес blank.html#access_token=… (старые приложения VK)."""
        parsed = parse_token_url(text)
        if not parsed:
            raise VKError("в сообщении нет access_token — пришли адрес страницы целиком")
        token, expires, user_id = parsed
        self._call("users.get", token=token)
        self.db.set("vk_token", token)
        self.db.set("vk_refresh_token", "")
        self.db.set("vk_scope_granted", "legacy")
        self.db.set("vk_user_id", user_id)
        until = (datetime.now(timezone.utc) + timedelta(seconds=expires)).isoformat(timespec="seconds") \
            if expires else ""
        self.db.set("vk_token_expires", until)
        text = self._connected_report(token, user_id, "ключ")
        return text + ("\nВход бессрочный." if not expires else
                       f"\n⚠️ VK выдал вход на {expires // 3600} ч — напомню заранее, когда понадобится войти снова.")

    def _connected_report(self, token: str, user_id: str, how: str, granted: str = "") -> str:
        user = self._call("users.get", token=token) or [{}]
        name = " ".join(x for x in (user[0].get("first_name"), user[0].get("last_name")) if x)
        lines = [f"✅ VK подключён ({how}): {name or 'аккаунт ' + user_id}."]
        if granted:
            missing = [x for x in ("wall", "video") if x not in granted.split()]
            lines.append(f"Доступы: {granted}" + (f" — ⚠️ не хватает: {', '.join(missing)}. Включи их в "
                                                  "кабинете VK ID → Доступы и войди снова (/vk)" if missing else ""))
        if self.cfg.vk_group_id:
            try:
                g = self._call("groups.getById", token=token, group_id=self.cfg.vk_group_id)
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
        """Предупреждение, если вход в VK скоро истечёт (за сутки) или не продлевается."""
        if self.db.get("vk_refresh_token"):
            _ = self.token  # заодно продлеваем
            err = self.db.get("vk_auth_error") or ""
            return f"⚠️ VK: не удалось продлить вход ({err}). Пройди /vk заново." if err else ""
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
        if not self.can_autopublish() and self.cfg.vk_manual:
            return "VK: полуавтомат — в час публикации пришлю файл и текст для VK Клипов"
        if not self.cfg.vk_app_id and not self.cfg.vk_access_token:
            return "VK: не подключён (нужны VK_APP_ID и VK_GROUP_ID, затем /vk)"
        if not self.ready() and not (self.db.get("vk_token") or self.db.get("vk_refresh_token")):
            return "VK: нужен вход — /vk"
        if self.db.get("vk_auth_error"):
            return f"VK: ⚠️ вход не продлевается ({self.db.get('vk_auth_error')[:120]}) — /vk"
        if self.db.get("vk_refresh_token"):
            return f"VK: подключён через VK ID, сообщество {self.cfg.vk_group_id or 'НЕ указано (VK_GROUP_ID)'}"
        if not self.cfg.vk_group_id:
            return "VK: вход есть, но не указано сообщество VK_GROUP_ID"
        exp = self.expires_at()
        return f"VK: подключён, сообщество {self.cfg.vk_group_id}" + (f", вход до {exp:%d.%m %H:%M} UTC" if exp else "")
