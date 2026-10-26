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
        # Если владелец вписал в переменные новый токен, старый продлённый не должен его перебивать.
        if self.db.get("ig_token") and self.db.get("ig_token_src") == self.cfg.ig_access_token:
            return self.db.get("ig_token")
        return self.cfg.ig_access_token

    def ready(self) -> bool:
        return bool(self.token and self.cfg.public_base_url)

    @property
    def account_id(self) -> str:
        """Номер профессионального аккаунта, к которому относится токен.

        В IG_USER_ID легко вписать не тот номер (id приложения, страницы, «app-scoped»
        id из ответа /me) — тогда Meta отвечает «Object with ID … does not exist
        (code 100, sub 33)». Поэтому номер берём у самого токена: GET /me → user_id.
        """
        src = self.token
        if self.db.get("ig_account_id") and self.db.get("ig_account_src") == src[-16:]:
            return self.db.get("ig_account_id")
        try:
            me = self._req("GET", "me", fields="user_id,username")
        except IGError as e:
            log.warning("ig /me: %s", e)
            return self.cfg.ig_user_id
        acc = str(me.get("user_id") or me.get("id") or self.cfg.ig_user_id)
        self.db.set("ig_account_id", acc)
        self.db.set("ig_account_src", src[-16:])
        self.db.set("ig_username", str(me.get("username") or ""))
        if self.cfg.ig_user_id and self.cfg.ig_user_id != acc:
            log.warning("IG_USER_ID %s не совпадает с аккаунтом токена %s — использую %s",
                        self.cfg.ig_user_id, acc, acc)
        return acc

    def check(self) -> str:
        """Строка для /status: к какому аккаунту привязан токен и годится ли он."""
        if not self.token:
            return "Instagram: не подключён — публикуешь вручную"
        self.db.set("ig_account_src", "")  # перепроверяем по-настоящему
        try:
            me = self._req("GET", "me", fields="user_id,username,account_type")
        except IGError as e:
            return f"Instagram: ⚠️ токен не работает: {e}"
        acc = str(me.get("user_id") or me.get("id"))
        self.db.set("ig_account_id", acc)
        self.db.set("ig_account_src", self.token[-16:])
        line = f"Instagram: подключён @{me.get('username')} (аккаунт {acc}, тип {me.get('account_type') or '?'})"
        if self.cfg.ig_user_id and self.cfg.ig_user_id != acc:
            line += f"; в IG_USER_ID стоит {self.cfg.ig_user_id} — неверный номер, использую {acc}"
        if not self.cfg.public_base_url:
            line += "; ⚠️ нет PUBLIC_BASE_URL — Instagram не сможет скачать видео"
        return line

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
        self.db.set("ig_token_src", self.cfg.ig_access_token)
        self.db.set("ig_token_refreshed_at", now_iso())
        return True

    # ----------------------------------------------------------- publish
    def media_url(self, token: str, ext: str) -> str:
        return f"{self.cfg.public_base_url}/media/{token}.{ext}"

    def publish(self, post_id: int, max_wait_s: int = 300, poll_s: int = 20) -> str:
        """Публикует одобренный пост. Возвращает ссылку на Reels.

        Любая ошибка переводит пост в 'failed' (с кнопкой повтора), а не
        оставляет его в 'publishing'. Если ролик уже вышел, его media_id
        сохраняется сразу, и повтор просто дозаполняет ссылку.
        """
        post = self.db.post(post_id)
        if not post:
            raise IGError(f"пост #{post_id} не найден")
        if not self.db.claim_post(post_id, "approved", "publishing"):
            raise IGError(f"пост #{post_id} не в статусе «одобрен» (сейчас: {post['status']})")
        media_id = post["ig_media_id"]
        try:
            if not media_id:
                media_id = self._create_and_publish(post_id, post, max_wait_s, poll_s)
        except Exception as e:  # noqa: BLE001 — любая ошибка не должна оставить пост «в публикации»
            msg = str(e) if isinstance(e, IGError) else f"{type(e).__name__}: {e}"
            msg = msg.replace(self.token, "***") if self.token else msg
            # Контейнер в статусе ERROR/EXPIRED переиспользовать нельзя — сбрасываем.
            reset = {"ig_container_id": None} if "не принял" in msg else {}
            self.db.update_post(post_id, status="failed", error=msg[:500], **reset)
            raise IGError(msg) from None
        try:
            permalink = self._req("GET", media_id, fields="permalink").get("permalink", "")
        except IGError as e:  # ролик уже вышел — ссылка не обязательна
            log.warning("permalink #%s: %s", post_id, e)
            permalink = ""
        self.db.update_post(post_id, status="published", ig_media_id=media_id, permalink=permalink,
                            published_at=post["published_at"] or now_iso(), error=None)
        return permalink or f"(ролик опубликован, id {media_id})"

    def _create_and_publish(self, post_id: int, post, max_wait_s: int, poll_s: int) -> str:
        container = post["ig_container_id"]
        if not container:
            container = self._req(
                "POST", f"{self.account_id}/media",
                media_type="REELS",
                video_url=self.media_url(post["media_token"], "mp4"),
                cover_url=self.media_url(post["media_token"], "jpg"),
                caption=post["caption"] or "",
                share_to_feed="true",
            ).get("id")
            if not container:
                raise IGError("Instagram не вернул id контейнера")
            self.db.update_post(post_id, ig_container_id=container)

        waited = 0
        while True:
            status = self._req("GET", container, fields="status_code,status").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise IGError(f"Instagram не принял видео: {status}")
            if status == "PUBLISHED":
                media_id = self._find_published(post["caption"] or "")
                if not media_id:
                    raise IGError("контейнер уже опубликован, но ролик в профиле не найден — проверь вручную")
                self.db.update_post(post_id, ig_media_id=media_id, published_at=now_iso())
                return media_id
            if waited >= max_wait_s:
                raise IGError("Instagram слишком долго обрабатывает видео")
            self.sleep(poll_s)
            waited += poll_s

        media_id = self._req("POST", f"{self.account_id}/media_publish", creation_id=container).get("id")
        if not media_id:
            raise IGError("Instagram не вернул id опубликованного ролика")
        # Сохраняем сразу: даже если дальше что-то упадёт, ролик не опубликуется второй раз.
        self.db.update_post(post_id, ig_media_id=media_id, published_at=now_iso())
        return media_id

    def _find_published(self, caption: str) -> str | None:
        """Ищет среди последних роликов профиля тот, что с нашим текстом."""
        items = self._req("GET", f"{self.account_id}/media", fields="id,caption", limit=15).get("data", [])
        want = " ".join(caption.split())
        for it in items:
            if " ".join((it.get("caption") or "").split()) == want:
                return it.get("id")
        return None

    # ----------------------------------------------------------- insights
    def insights(self, media_id: str) -> dict[str, int | float | None]:
        """Метрики ролика. Если какая-то метрика не поддерживается, запрашивает по одной."""
        bad = self._bad_metrics()
        metrics = [m for m in METRICS if m not in bad]
        try:
            data = self._req("GET", f"{media_id}/insights", metric=",".join(metrics)).get("data", [])
        except IGError:
            data, failed = [], []
            for m in metrics:
                try:
                    data += self._req("GET", f"{media_id}/insights", metric=m).get("data", [])
                except IGError as e:
                    failed.append(m)
                    log.debug("metric %s unavailable: %s", m, e)
            if not data:
                # Не пришла ни одна метрика — проблема в самом ролике (удалён) или в доступе,
                # а не в метриках. Ничего не запоминаем.
                raise IGError(f"нет статистики по ролику {media_id}")
            if failed:
                self.db.set("ig_bad_metrics", ",".join(sorted(bad | set(failed))))
                self.db.set("ig_bad_metrics_at", now_iso())
        raw = {d["name"]: (d.get("values") or [{}])[0].get("value") for d in data}
        return {
            "plays": raw.get("views"), "reach": raw.get("reach"), "likes": raw.get("likes"),
            "comments": raw.get("comments"), "saves": raw.get("saved"), "shares": raw.get("shares"),
            "avg_watch_ms": raw.get("ig_reels_avg_watch_time"),
            "total_watch_ms": raw.get("ig_reels_video_view_total_time"),
            "skip_rate": raw.get("reels_skip_rate"),
        }

    def _bad_metrics(self) -> set[str]:
        """Метрики, которые API отверг. Список забывается через 7 дней: Meta может их вернуть."""
        at = self.db.get("ig_bad_metrics_at")
        if not at or datetime.now(timezone.utc) - datetime.fromisoformat(at) > timedelta(days=7):
            return set()
        return set(filter(None, (self.db.get("ig_bad_metrics") or "").split(",")))

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
