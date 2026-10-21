"""HTTP-сервер: отдаёт готовые ролики Instagram'у и отвечает на проверку здоровья.

Отдаются только файлы вида /media/<случайный_токен>.mp4|.jpg — список
файлов посмотреть нельзя, а токен не угадать.
"""
from __future__ import annotations

import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

MEDIA_RE = re.compile(r"^/media/([A-Za-z0-9_-]{16,64})\.(mp4|jpg)$")
MEDIA_TOKEN_RE = re.compile(r"/media/[A-Za-z0-9_-]+")
TYPES = {"mp4": "video/mp4", "jpg": "image/jpeg"}
CHUNK = 256 * 1024


def make_handler(media_dir: Path, routes: dict | None = None):
    """routes: {путь: функция(параметры запроса) -> HTML} — например, возврат после входа в VK."""
    routes = routes or {}

    class Handler(BaseHTTPRequestHandler):
        server_version = "content-engine"

        def log_message(self, fmt, *args):  # в логи Railway — без токенов роликов и кодов входа
            line = MEDIA_TOKEN_RE.sub("/media/***", fmt % args)
            log.info("%s %s", self.address_string(), re.sub(r"\?[^ ]*", "?***", line))

        def _send_file(self, head_only: bool):
            m = MEDIA_RE.match(self.path.split("?", 1)[0])
            if not m:
                self.send_error(404)
                return
            path = media_dir / f"{m.group(1)}.{m.group(2)}"
            if not path.is_file():
                self.send_error(404)
                return
            size = path.stat().st_size
            start, end = 0, size - 1
            rng = self.headers.get("Range")
            if rng and (rm := re.match(r"bytes=(\d*)-(\d*)$", rng.strip())):
                if rm.group(1):
                    start = int(rm.group(1))
                    end = int(rm.group(2)) if rm.group(2) else size - 1
                elif rm.group(2):  # bytes=-N — последние N байт
                    start = max(0, size - int(rm.group(2)))
                end = min(end, size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", TYPES[m.group(2)])
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            if head_only:
                return
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path in routes:
                from urllib.parse import parse_qs
                params = {k: v[0] for k, v in parse_qs(query).items()}
                try:
                    html = routes[path](params)
                except Exception:  # noqa: BLE001
                    log.exception("route %s", path)
                    html = "<h3>Ошибка. Подробности — в админ-боте.</h3>"
                body = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                        f"<body style='font-family:sans-serif;padding:24px'>{html}</body>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path in ("/", "/health"):
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_file(head_only=False)

        def do_HEAD(self):
            self._send_file(head_only=True)

    return Handler


def start(media_dir: Path, port: int, host: str = "0.0.0.0", routes: dict | None = None) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(media_dir, routes))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    log.info("HTTP-сервер слушает порт %s", port)
    return srv


def legal_pages(bot: str) -> dict:
    """Страницы приложения для Google (Branding): главная, политика конфиденциальности, условия."""
    bot = bot.lstrip("@")
    home = (f"<h2>AI-Photo — контент-движок Easyfotobot</h2>"
            f"<p>Служебное приложение владельца Telegram-бота <a href='https://t.me/{bot}'>@{bot}</a>: "
            "публикует короткие ролики владельца в его собственные аккаунты (YouTube Shorts, Instagram, VK) "
            "после его одобрения и собирает статистику этих роликов.</p>"
            "<p><a href='/privacy'>Политика конфиденциальности</a> · <a href='/terms'>Условия использования</a></p>")
    privacy = ("<h2>Политика конфиденциальности</h2>"
               "<p>Приложение используется только владельцем для публикации собственных роликов в его аккаунты.</p>"
               "<p><b>Какие данные используются.</b> Ключи доступа к аккаунтам владельца (YouTube, Instagram, VK), "
               "которые он выдал при входе; название канала; статистика опубликованных роликов "
               "(просмотры, лайки, комментарии).</p>"
               "<p><b>Как используются.</b> Только для загрузки роликов, которые владелец одобрил, и для подсчёта "
               "их статистики. Данные YouTube не передаются третьим лицам, не продаются и не используются для рекламы.</p>"
               "<p><b>Хранение.</b> Ключи хранятся в закрытой базе приложения на сервере владельца. "
               "Доступ можно отозвать в любой момент: myaccount.google.com → Безопасность → Сторонние приложения; "
               "после отзыва ключи перестают действовать.</p>"
               "<p>Приложение использует YouTube API Services; действуют "
               "<a href='https://www.youtube.com/t/terms'>Условия использования YouTube</a> и "
               "<a href='https://policies.google.com/privacy'>Политика конфиденциальности Google</a>.</p>"
               f"<p>Связь с владельцем: <a href='https://t.me/{bot}'>@{bot}</a>.</p>")
    terms = ("<h2>Условия использования</h2>"
             "<p>Приложение — служебный инструмент владельца для публикации его собственных роликов. "
             "Сторонние пользователи им не пользуются. Используя вход через Google, вы соглашаетесь с "
             "<a href='https://www.youtube.com/t/terms'>Условиями использования YouTube</a>.</p>")
    return {"/home": lambda _: home, "/privacy": lambda _: privacy, "/terms": lambda _: terms}

