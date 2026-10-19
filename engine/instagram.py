"""Публикация Reels и сбор статистики через Instagram API with Instagram Login.

Как устроено у Meta: создаём «контейнер» со ссылкой на видео (Instagram
сам скачивает его с нашего сервера), ждём, пока он обработается,
и публикуем. Нужны права instagram_business_basic и
instagram_business_content_publish (для статистики — instagram_business_manage_insights).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import requests

from .config import Config
from .db import DB, now_iso

log = logging.getLogger(__name__)

HOST = "https://graph.instagram.com"
METRICS = ["views", "reach", "likes", "comments", "saved", "shares",
           "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "reels_skip_rate"]


class IGError(RuntimeError):
    pass


class Instagram:
    def __init__(self, cfg: Config, db: DB, sleep=time.sleep):
        self.cfg = cfg
        self.db = db
        self.sleep = sleep

    # ------------------------------------------------------------- token
    @property
    def token(self) -> str:
        # Обновлённый токен хранится в базе и важнее исходного из переменных.
        return self.db.get("ig_token") or self.cfg.ig_access_token

    def ready(self) -> bool:
        return bool(self.cfg.ig_user_id and self.token and self.cfg.public_base_url)

    def _url(self, path: str) -> str:
        return f"{HOST}/{self.cfg.ig_graph_version}/{path.lstrip('/')}"

    def _req(self, method: str, path: str, **params) -> dict:
        params["access_token"] = self.token
        try:
            if method == "GET":
                r = requests.get(self._url(path), params=params, timeout=60)
            else:
                r = requests.post(self._url(path), data=params, timeout=60)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise IGError(str(e).replace(self.token, "***")) from None
        if "error" in data:
            err = data["error"]
            raise IGError(f"{err.get('message')} (code {err.get('code')}, sub {err.get('error_subcode')})")
        return data

    def refresh_token(self) -> bool:
        """Продлевает долгоживущий токен (живёт 60 дней). Вызывать раз в неделю."""
        try:
            r = requests.get(f"{HOST}/refresh_access_token",
                             params={"grant_type": "ig_refresh_token", "access_token": self.token}, timeout=60)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            log.error("token refresh failed: %s", str(e).replace(self.token, "***"))
            return False
        if "access_token" not in data:
            log.error("token refresh failed: %s", data.get("error"))
            return False
        self.db.set("ig_token", data["access_token"])
        self.db.set("ig_token_refreshed_at", now_iso())
        return True

    # ----------------------------------------------------------- publish
    def media_url(self, token: str, ext: str) -> str:
        return f"{self.cfg.public_base_url}/media/{token}.{ext}"

    def publish(self, post_id: int, max_wait_s: int = 300, poll_s: int = 20) -> str:
        """Публикует одобренный пост. Возвращает ссылку на Reels."""
        post = self.db.post(post_id)
        if not post:
            raise IGError(f"пост #{post_id} не найден")
        if not self.db.claim_post(post_id, "approved", "publishing"):
            raise IGError(f"пост #{post_id} не в статусе «одобрен» (сейчас: {post['status']})")
        try:
            container = post["ig_container_id"]
            if not container:
                container = self._req(
                    "POST", f"{self.cfg.ig_user_id}/media",
                    media_type="REELS",
                    video_url=self.media_url(post["media_token"], "mp4"),
                    cover_url=self.media_url(post["media_token"], "jpg"),
                    caption=post["caption"] or "",
                    share_to_feed="true",
                )["id"]
                self.db.update_post(post_id, ig_container_id=container)

            waited = 0
            while True:
                status = self._req("GET", container, fields="status_code,status").get("status_code")
                if status == "FINISHED":
                    break
                if status in ("ERROR", "EXPIRED"):
                    raise IGError(f"Instagram не принял видео: {status}")
                if status == "PUBLISHED":
                    break
                if waited >= max_wait_s:
                    raise IGError("Instagram слишком долго обрабатывает видео")
                self.sleep(poll_s)
                waited += poll_s

            media_id = self._req("POST", f"{self.cfg.ig_user_id}/media_publish", creation_id=container)["id"]
            permalink = self._req("GET", media_id, fields="permalink").get("permalink", "")
        except IGError as e:
            # Контейнер в статусе ERROR/EXPIRED переиспользовать нельзя — сбрасываем.
            reset = {"ig_container_id": None} if "не принял" in str(e) else {}
            self.db.update_post(post_id, status="failed", error=str(e)[:500], **reset)
            raise
        self.db.update_post(post_id, status="published", ig_media_id=media_id, permalink=permalink,
                            published_at=now_iso(), error=None)
        return permalink

    # ----------------------------------------------------------- insights
    def insights(self, media_id: str) -> dict[str, int | float | None]:
        """Метрики ролика. Если какая-то метрика не поддерживается, запрашивает по одной."""
        try:
            data = self._req("GET", f"{media_id}/insights", metric=",".join(METRICS))["data"]
        except IGError:
            data = []
            for m in METRICS:
                try:
                    data += self._req("GET", f"{media_id}/insights", metric=m)["data"]
                except IGError as e:
                    log.debug("metric %s unavailable: %s", m, e)
        raw = {d["name"]: (d.get("values") or [{}])[0].get("value") for d in data}
        return {
            "plays": raw.get("views"), "reach": raw.get("reach"), "likes": raw.get("likes"),
            "comments": raw.get("comments"), "saves": raw.get("saved"), "shares": raw.get("shares"),
            "avg_watch_ms": raw.get("ig_reels_avg_watch_time"),
            "total_watch_ms": raw.get("ig_reels_video_view_total_time"),
            "skip_rate": raw.get("reels_skip_rate"),
        }

    def collect_all(self, max_age_days: int = 30) -> int:
        """Снимает метрики со всех опубликованных за max_age_days роликов."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
        rows = self.db.q("SELECT id, ig_media_id FROM posts WHERE status='published' "
                         "AND ig_media_id IS NOT NULL AND published_at >= ?", (cutoff,))
        n = 0
        for r in rows:
            try:
                self.db.add_metrics(r["id"], self.insights(r["ig_media_id"]))
                n += 1
            except IGError as e:
                log.warning("insights #%s: %s", r["id"], e)
        return n
