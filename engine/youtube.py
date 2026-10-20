"""Публикация роликов в YouTube Shorts через официальный YouTube Data API.

Подключение один раз: в Google Cloud — YouTube Data API v3, экран согласия
(OAuth consent screen) в статусе «In production» (иначе Google отзывает вход
через 7 дней) и OAuth-клиент типа «Web application» с адресом возврата
<PUBLIC_BASE_URL>/yt/callback. Его id и секрет — в YT_CLIENT_ID и YT_CLIENT_SECRET.
Команда /youtube даёт ссылку входа; Google возвращает на сервер движка, ключи
сохраняются в базе движка и продлеваются сами.

Вертикальное видео до 3 минут YouTube сам показывает как Shorts. Ссылки в
описании Shorts не кликабельны, поэтому главная ссылка на бот — в шапке канала;
в описании оставляем ссылку с меткой utm_yt_short_p<id> текстом.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

from . import funnel
from .config import Config
from .db import DB

log = logging.getLogger(__name__)

AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
REFRESH_EARLY = timedelta(minutes=5)


class YTError(RuntimeError):
    pass


def redirect_uri(cfg: Config) -> str:
    return f"{cfg.public_base_url}/yt/callback"


def _error_text(r) -> str:
    try:
        d = r.json()
    except ValueError:
        return f"код {r.status_code}"
    err = d.get("error")
    if isinstance(err, dict):
        reason = ((err.get("errors") or [{}])[0]).get("reason") or ""
        msg = err.get("message") or ""
        hints = {
            "quotaExceeded": "дневная квота YouTube API исчерпана — продолжу завтра",
            "uploadLimitExceeded": "YouTube ограничил число загрузок на канале — продолжу позже",
            "youtubeSignupRequired": "у аккаунта нет канала YouTube — создай канал и войди снова (/youtube)",
            "forbidden": "нет прав на загрузку — войди снова (/youtube) и разреши загрузку видео",
        }
        return hints.get(reason, f"{msg or reason} (код {r.status_code})")
    return f"{d.get('error_description') or err or 'ошибка'} (код {r.status_code})"


class YouTube:
    def __init__(self, cfg: Config, db: DB):
        self.cfg, self.db = cfg, db
        self._lock = threading.Lock()

    # ------------------------------------------------------------ вход
    def configured(self) -> bool:
        return bool(self.cfg.yt_client_id and self.cfg.yt_client_secret)

    def ready(self) -> bool:
        return self.configured() and bool(self.db.get("yt_refresh_token"))

    def start_login(self) -> str:
        if not self.configured():
            raise YTError("не заданы YT_CLIENT_ID и YT_CLIENT_SECRET")
        if not self.cfg.public_base_url:
            raise YTError("не задан PUBLIC_BASE_URL — адрес сервиса движка в Railway")
        state = secrets.token_urlsafe(32)
        self.db.set("yt_state", state)
        q = {"client_id": self.cfg.yt_client_id, "redirect_uri": redirect_uri(self.cfg),
             "response_type": "code", "scope": SCOPES, "access_type": "offline",
             "prompt": "consent", "include_granted_scopes": "true", "state": state}
        return f"{AUTH}?{urlencode(q)}"

    def _token_request(self, data: dict) -> dict:
        data = {**data, "client_id": self.cfg.yt_client_id, "client_secret": self.cfg.yt_client_secret}
        try:
            r = requests.post(TOKEN, data=data, timeout=30)
        except requests.RequestException as e:
            raise YTError(f"Google недоступен ({type(e).__name__})") from None
        if r.status_code != 200:
            raise YTError(f"Google: {_error_text(r)}")
        return r.json()

    def _store(self, d: dict) -> None:
        self.db.set("yt_token", d["access_token"])
        if d.get("refresh_token"):
            self.db.set("yt_refresh_token", d["refresh_token"])
        exp = int(d.get("expires_in") or 3600)
        self.db.set("yt_token_expires",
                    (datetime.now(timezone.utc) + timedelta(seconds=exp)).isoformat(timespec="seconds"))
        self.db.set("yt_auth_error", "")

    def finish_login(self, params: dict) -> str:
        if params.get("error"):
            raise YTError(params.get("error_description") or params["error"])
        state = self.db.get("yt_state") or ""
        if not state or params.get("state") != state:
            raise YTError("ссылка входа устарела — набери /youtube и войди по новой ссылке")
        if not params.get("code"):
            raise YTError("Google не вернул код входа")
        d = self._token_request({"grant_type": "authorization_code", "code": params["code"],
                                 "redirect_uri": redirect_uri(self.cfg)})
        self.db.set("yt_state", "")
        if not d.get("refresh_token") and not self.db.get("yt_refresh_token"):
            raise YTError("Google не выдал постоянный ключ — убери доступ приложения в аккаунте Google "
                          "(myaccount.google.com → Безопасность → Сторонние приложения) и войди снова")
        self._store(d)
        granted = d.get("scope") or ""
        lines = []
        try:
            ch = self._api("GET", "channels", part="snippet", mine="true").get("items") or []
            title = ch[0]["snippet"]["title"] if ch else ""
            lines.append(f"✅ YouTube подключён: канал «{title}»." if title else
                         "⚠️ Вход есть, но у аккаунта нет канала YouTube — создай канал и войди снова (/youtube).")
            if ch:
                self.db.set("yt_channel", title)
        except YTError as e:
            lines.append(f"✅ Вход в Google есть, но канал проверить не удалось: {e}")
        if "youtube.upload" not in granted:
            lines.append("⚠️ Не выдано право на загрузку видео — войди снова и отметь все галочки.")
        lines.append("Ролики будут выходить в Shorts в те же часы, что и в Instagram.")
        return "\n".join(lines)

    @property
    def token(self) -> str:
        with self._lock:
            raw = self.db.get("yt_token_expires") or ""
            exp = datetime.fromisoformat(raw) if raw else None
            if not self.db.get("yt_token") or not exp or exp - datetime.now(timezone.utc) < REFRESH_EARLY:
                try:
                    self._store(self._token_request({"grant_type": "refresh_token",
                                                     "refresh_token": self.db.get("yt_refresh_token") or ""}))
                except YTError as e:
                    self.db.set("yt_auth_error", str(e)[:300])
                    raise
        return self.db.get("yt_token") or ""

    # ------------------------------------------------------------ API
    def _api(self, method: str, path: str, **params) -> dict:
        try:
            r = requests.request(method, f"{API}/{path}", params=params,
                                 headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        except requests.RequestException as e:
            raise YTError(f"YouTube недоступен ({type(e).__name__})") from None
        if r.status_code >= 400:
            raise YTError(f"YouTube: {_error_text(r)}")
        return r.json()

    def text_for(self, post) -> tuple[str, str]:
        plan = json.loads(post["script_json"] or "{}")
        hook = str(plan.get("script", {}).get("hook") or "").replace("*", "").strip()
        title = (hook or "Нейрофото из обычных селфи")[:90].rstrip() + " #shorts"
        link = funnel.link(self.cfg, "yt", "short", f"p{post['id']}")
        caption = (post["caption"] or "").strip()
        desc = f"{caption}\n\n👉 Бот — ссылка в шапке канала. Или: {link}"
        return title[:100], desc[:4900]

    def publish(self, post_id: int) -> str:
        post = self.db.post(post_id)
        if not post:
            raise YTError(f"пост #{post_id} не найден")
        row = self.db.crosspost(post_id, "yt")
        if row and row["external_id"]:  # уже загружено — не дублируем
            return f"https://youtube.com/shorts/{row['external_id']}"
        video = Path(post["video_path"] or "")
        if not post["video_path"] or not video.exists():
            raise YTError("файл ролика уже удалён с тома")
        title, desc = self.text_for(post)
        meta = {"snippet": {"title": title, "description": desc, "categoryId": "26",
                            "tags": ["нейрофото", "нейрофотосессия", "ai фото", "shorts"],
                            "defaultLanguage": "ru", "defaultAudioLanguage": "ru"},
                "status": {"privacyStatus": self.cfg.yt_privacy, "selfDeclaredMadeForKids": False}}
        token = self.token
        try:
            r = requests.post(f"{UPLOAD}?uploadType=resumable&part=snippet,status", json=meta, timeout=60,
                              headers={"Authorization": f"Bearer {token}",
                                       "X-Upload-Content-Type": "video/mp4",
                                       "X-Upload-Content-Length": str(video.stat().st_size)})
            if r.status_code >= 400 or not r.headers.get("Location"):
                raise YTError(f"YouTube: {_error_text(r)}")
            with open(video, "rb") as f:
                up = requests.put(r.headers["Location"], data=f, timeout=900,
                                  headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"})
        except requests.RequestException as e:
            raise YTError(f"загрузка в YouTube не удалась ({type(e).__name__})") from None
        if up.status_code not in (200, 201):
            raise YTError(f"YouTube: {_error_text(up)}")
        vid = up.json().get("id")
        if not vid:
            raise YTError("YouTube не вернул id ролика")
        url = f"https://youtube.com/shorts/{vid}"
        self.db.update_crosspost(post_id, "yt", external_id=vid, url=url, error=None)
        return url

    def stats(self, ids: list[str]) -> dict[str, dict]:
        """Просмотры, лайки, комментарии по id роликов (до 50 за раз)."""
        out = {}
        for i in range(0, len(ids), 50):
            d = self._api("GET", "videos", part="statistics", id=",".join(ids[i:i + 50]))
            for it in d.get("items", []):
                s = it.get("statistics", {})
                out[it["id"]] = {k: int(s[k]) if s.get(k) is not None else None
                                 for k in ("viewCount", "likeCount", "commentCount")}
        return out

    def status(self) -> str:
        if not self.configured():
            return "YouTube: не подключён (нужны YT_CLIENT_ID и YT_CLIENT_SECRET, затем /youtube)"
        if not self.ready():
            return "YouTube: нужен вход — /youtube"
        if self.db.get("yt_auth_error"):
            return f"YouTube: ⚠️ вход не продлевается ({self.db.get('yt_auth_error')[:120]}) — /youtube"
        ch = self.db.get("yt_channel") or ""
        return "YouTube: подключён" + (f", канал «{ch}»" if ch else "")
