"""Точка входа: связывает модули и крутит расписание.

Поток 1 — админ-бот (long polling Telegram).
Поток 2 — планировщик: раз в 30 секунд проверяет, не пора ли что-то сделать.
Поток 3 — HTTP-сервер раздачи видео для Instagram.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import analyst, images, occasions, playbook as pbm, scout, server
from .admin_bot import AdminBot, Services, retry_keyboard
from .config import Config
from .db import DB
from .instagram import IGError, Instagram
from .llm import AI
from .producer import produce
from .telegram import TelegramAPI

log = logging.getLogger("engine")

SLOT_FMT = "%Y-%m-%d %H:%M"
LIVE = ("rendered", "sending", "pending", "approved", "publishing", "published", "manual", "failed")


class Engine:
    def __init__(self, cfg: Config, db: DB, ai: AI, ig: Instagram,
                 bot: AdminBot | None = None, now: Callable[[], datetime] | None = None,
                 rng: random.Random | None = None):
        self.cfg, self.db, self.ai, self.ig, self.bot = cfg, db, ai, ig, bot
        self.tz = ZoneInfo(cfg.timezone)
        self._now = now or (lambda: datetime.now(self.tz))
        self.rng = rng or random.Random()

    # ---------------------------------------------------------- utilities
    def now(self) -> datetime:
        return self._now().astimezone(self.tz)

    def say(self, text: str, markup: dict | None = None) -> None:
        if self.bot:
            self.bot.say(text, markup)

    def slots(self, day: date) -> list[datetime]:
        hours = self.cfg.publish_hours_list[: max(1, self.cfg.reels_per_day)]
        return [datetime(day.year, day.month, day.day, h, tzinfo=self.tz) for h in hours]

    def slot_taken(self, slot: datetime) -> bool:
        marks = ",".join("?" * len(LIVE))
        return bool(self.db.one(f"SELECT 1 FROM posts WHERE scheduled_for=? AND status IN ({marks})",
                                (slot.strftime(SLOT_FMT), *LIVE)))

    def _once(self, key: str) -> bool:
        """True, если задача с таким ключом ещё не выполнялась (и помечает её)."""
        if self.db.get(f"job:{key}"):
            return False
        self.db.set(f"job:{key}", self.now().isoformat(timespec="seconds"))
        return True

    def _every(self, name: str, period: timedelta) -> bool:
        last = self.db.get(f"every:{name}")
        now = self.now()
        if last and now - datetime.fromisoformat(last) < period:
            return False
        self.db.set(f"every:{name}", now.isoformat(timespec="seconds"))
        return True

    # -------------------------------------------------------------- actions
    def make_one(self, scheduled_for: str | None = None, occasion=None) -> int:
        pb = pbm.load(self.cfg.playbook_path)
        return produce(self.cfg, self.db, self.ai, pb, self.now().date(), self.rng, scheduled_for,
                       occasion=occasion)

    # ------------------------------------------------------------- поводы
    def _events(self) -> list[dict]:
        return pbm.load(self.cfg.playbook_path).get("events", [])

    def occasion_reels(self, occ, since_days: int) -> int:
        cutoff = (self.now() - timedelta(days=since_days)).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        return self.db.one("SELECT COUNT(*) n FROM posts WHERE theme=? AND status NOT IN ('rejected') "
                           "AND created_at >= ?", (occ.theme_id, cutoff))["n"]

    def occasion_for_today(self, made_today: int):
        """Повод для очередного ролика дня или None. Не больше occasions_per_day в день
        и не больше max_reels на один повод за всё его окно."""
        if made_today >= self.cfg.occasions_per_day:
            return None
        for occ in occasions.active(self.now().date(), self._events()):
            if self.occasion_reels(occ, occ.lead + occ.after + 1) < occ.max_reels:
                return occ
        return None

    def make_for(self, query: str) -> int:
        """/make <повод>: «повод» — ближайший активный, иначе поиск по названию."""
        today = self.now().date()
        q = query.strip().lower()
        if q in ("повод", "праздник"):
            act = occasions.active(today, self._events())
            occ = act[0] if act else None
        else:
            occ = occasions.find(q, today, self._events())
        if not occ:
            raise ValueError(f"не нашёл повод «{query}». Ближайшие поводы — /occasions")
        return self.make_one(occasion=occ)

    def occasions_text(self) -> str:
        today = self.now().date()
        events = self._events()
        act = {o.id for o in occasions.active(today, events)}
        items = occasions.upcoming(today, 30, events)
        if not items:
            return "В ближайшие 30 дней поводов в календаре нет."
        lines = ["Поводы на 30 дней (● — уже идут ролики к поводу):"]
        for o in items:
            mark = "●" if o.id in act else "○"
            src = " · инфоповод" if o.source == "trend" else ""
            lines.append(f"{mark} {o.day:%d.%m} — {o.title}{src}")
        lines.append("\nСобрать ролик к поводу сейчас: /make повод (ближайший) или /make <название>, "
                     "например /make хэллоуин")
        return "\n".join(lines)

    def publish(self, post_id: int) -> str:
        try:
            link = self.ig.publish(post_id)
        except IGError as e:
            self.say(f"⚠️ Ролик #{post_id} не опубликован: {e}", retry_keyboard(post_id))
            return ""
        return f"🚀 Ролик #{post_id} опубликован: {link}"

    def recover(self) -> int:
        """После перезапуска: возвращает «зависшие» посты в рабочие статусы."""
        n = 0
        if not self.db.get("migr:tag_attempts_reset_1"):
            # В прошлой версии общий сбой разметки засчитывался каждому фото и выключал их.
            self.db.x("UPDATE assets SET tag_attempts=0 WHERE title IS NULL")
            self.db.set("migr:tag_attempts_reset_1", "1")
        for p in self.db.posts("publishing"):
            self.db.update_post(p["id"], status="failed", error="сервис перезапустился во время публикации")
            self.say(f"⚠️ Ролик #{p['id']} публиковался в момент перезапуска сервиса. "
                     "Проверь профиль: если ролика там нет — нажми «Повторить».", retry_keyboard(p["id"]))
            n += 1
        for p in self.db.posts("sending"):
            self.db.update_post(p["id"], status="rendered")
            n += 1
        return n

    # ----------------------------------------------------------------- jobs
    def job_plan(self) -> int:
        """Утром собирает ролики на сегодняшние свободные слоты и шлёт на одобрение."""
        now = self.now()
        if now.hour < self.cfg.plan_hour:
            return 0
        made = 0
        occ_today = self._occasion_posts_today()
        for slot in self.slots(now.date()):
            if slot <= now or self.slot_taken(slot):
                continue
            key = f"plan:{slot.strftime(SLOT_FMT)}"
            if not self._once(key):
                continue  # уже пытались (например, ролик отклонили) — не навязываем снова
            occ = self.occasion_for_today(occ_today)
            try:
                try:
                    pid = self.make_one(slot.strftime(SLOT_FMT), occasion=occ)
                    occ_today += 1 if occ else 0
                except Exception as e:  # noqa: BLE001
                    if not occ:
                        raise
                    # к поводу не получилось (например, генерация недоступна) — обычный ролик
                    log.warning("ролик к поводу «%s» не собрался: %s", occ.title, e)
                    occ_today = self.cfg.occasions_per_day  # сегодня к поводам больше не возвращаемся
                    self.say(f"⚠️ Ролик к поводу «{occ.title}» не собрался: {str(e)[:300]}\nСобираю обычный.")
                    pid = self.make_one(slot.strftime(SLOT_FMT))
            except Exception as e:  # noqa: BLE001
                log.exception("plan failed")
                self.say(f"⚠️ Не собрал ролик на {slot:%H:%M}: {e}\nМожно собрать вручную: /make")
                continue
            made += 1
            if self.bot:
                self.bot.send_for_approval(pid)
        return made

    def _occasion_posts_today(self) -> int:
        start = datetime(self.now().year, self.now().month, self.now().day, tzinfo=self.tz)
        return self.db.one("SELECT COUNT(*) n FROM posts WHERE theme LIKE 'occ:%' AND created_at >= ?",
                           (start.astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds"),))["n"]

    def next_free_slot(self) -> datetime:
        now = self.now()
        for d in range(0, 30):
            for slot in self.slots(now.date() + timedelta(days=d)):
                if slot > now and not self.slot_taken(slot):
                    return slot
        return now + timedelta(hours=1)

    def job_publish(self) -> int:
        """Публикует одобренные ролики, время которых наступило. Не больше одного за раз."""
        if self.db.get("paused") == "1" or not self.ig.ready():
            return 0
        for p in self.db.posts("approved"):
            if not p["scheduled_for"]:
                slot = self.next_free_slot()
                self.db.update_post(p["id"], scheduled_for=slot.strftime(SLOT_FMT))
                self.say(f"Ролик #{p['id']} встал в расписание на {slot:%d.%m %H:%M}.")
        now = self.now()
        due = [p for p in self.db.posts("approved")
               if datetime.strptime(p["scheduled_for"], SLOT_FMT).replace(tzinfo=self.tz) <= now]
        if not due:
            return 0
        result = self.publish(due[0]["id"])
        if result:
            self.say(result)
        return 1

    def job_tag(self) -> int:
        images.scan(self.cfg, self.db)
        if not (self.cfg.gemini_api_key or self.cfg.openai_api_key):
            if images.untagged_count(self.db) and self._once("warn:no_ai_key"):
                self.say("⚠️ Не задан ни GEMINI_API_KEY, ни OPENAI_API_KEY — фото не размечаются, "
                         "сценарии пишутся по шаблону. Добавь ключ в переменные Railway.")
            return 0
        try:
            n = images.tag_library(self.cfg, self.db, self.ai, pbm.load(self.cfg.playbook_path)["themes"], limit=10)
        except images.TaggingError as e:
            # Сообщаем один раз на каждую новую ошибку, а не каждые 5 минут.
            if self.db.get("last_tag_error") != str(e)[:300]:
                self.say(f"⚠️ {e}\nРолики при этом собираются и из неразмеченных фото. "
                         "Если ошибка про ключ или модель — проверь переменные Railway.")
            self.db.set("last_tag_error", str(e)[:300])
            return 0
        if n:
            self.db.set("last_tag_error", "")
            if not images.untagged_count(self.db):
                ready = self.db.one("SELECT COUNT(*) n FROM assets WHERE kind='library' AND title IS NOT NULL")["n"]
                self.say(f"Разметил фото библиотеки. Готово к роликам: {ready}.")
        return n

    def job_push(self) -> int:
        """Досылает на одобрение ролики, которые не ушли из-за сбоя Telegram."""
        return self.bot.push_rendered() if self.bot else 0

    def job_metrics(self) -> int:
        return self.ig.collect_all() if self.ig.ready() else 0

    def job_scout(self) -> bool:
        """Разведка трендов: при первом запуске и затем по понедельникам с 9:00 (до анализа в 10)."""
        if not self.cfg.gemini_api_key:
            return False
        now = self.now()
        if self.db.get("scout_last"):
            if now.weekday() != 0 or now.hour < 9:
                return False
            if not self._once(f"scout:{now:%G-W%V}"):
                return False
        report = scout.run(self.cfg, self.db, self.ai, today=now.date())
        if report.startswith("⚠️"):
            # без успешной разведки повторяем раз в час, но о той же ошибке говорим один раз
            if self.db.get("last_scout_error") != report:
                self.say(report)
            self.db.set("last_scout_error", report)
            return False
        self.db.set("last_scout_error", "")
        self.say(report)
        return True

    def job_analysis(self) -> bool:
        now = self.now()
        if now.weekday() != 0 or now.hour < 10:
            return False
        if not self._once(f"analysis:{now:%G-W%V}"):
            return False
        self.say(analyst.run(self.cfg, self.db, self.ai))
        if self.bot:
            self.bot.send_brief_proposals()
        return True

    def job_cleanup(self, keep_days: int = 7) -> int:
        """Удаляет файлы роликов, которые больше не нужны: отклонённые и давно опубликованные.

        Instagram хранит свою копию, а том Railway не бесконечный.
        """
        cutoff = (self.now() - timedelta(days=keep_days)).astimezone(ZoneInfo("UTC")).isoformat(timespec="seconds")
        rows = self.db.q(
            "SELECT id, video_path, cover_path FROM posts WHERE video_path IS NOT NULL AND ("
            "(status='rejected') OR (status IN ('published','manual') AND COALESCE(published_at, created_at) < ?))",
            (cutoff,))
        n = 0
        for r in rows:
            for f in (r["video_path"], r["cover_path"]):
                if f:
                    Path(f).unlink(missing_ok=True)
            self.db.update_post(r["id"], video_path=None, cover_path=None)
            n += 1
        return n

    def job_token(self) -> bool:
        if not self.ig.ready():
            return False
        if not self.db.get("ig_token_seen_at"):
            # Свежий токен нельзя продлить раньше чем через сутки — первый раз только запоминаем.
            self.db.set("ig_token_seen_at", self.now().isoformat(timespec="seconds"))
            return False
        ok = self.ig.refresh_token()
        if not ok:
            self.say("⚠️ Не удалось продлить токен Instagram. Если не исправить, через ~60 дней публикация остановится.")
        return ok

    def tick(self) -> None:
        jobs = [
            ("tag", timedelta(minutes=5), self.job_tag),
            ("plan", timedelta(minutes=10), self.job_plan),
            ("push", timedelta(minutes=5), self.job_push),
            ("publish", timedelta(seconds=30), self.job_publish),
            ("metrics", timedelta(hours=6), self.job_metrics),
            ("scout", timedelta(hours=1), self.job_scout),
            ("analysis", timedelta(hours=1), self.job_analysis),
            ("token", timedelta(days=7) if self.db.get("ig_token_seen_at") else timedelta(days=2), self.job_token),
            ("cleanup", timedelta(hours=12), self.job_cleanup),
        ]
        for name, period, fn in jobs:
            if not self._every(name, period):
                continue
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", name)
                self.say(f"⚠️ Задача «{name}» упала: {e}")


def build(cfg: Config) -> tuple[Engine, AdminBot | None]:
    cfg.ensure_dirs()
    db = DB(cfg.db_path)
    ai = AI(cfg)
    ig = Instagram(cfg, db)
    engine = Engine(cfg, db, ai, ig)
    bot = None
    if cfg.admin_bot_token:
        worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bot-job")

        def run_bg(fn):
            def safe():
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    log.exception("фоновая задача бота упала")
            return worker.submit(safe)

        bot = AdminBot(cfg, db, TelegramAPI(cfg.admin_bot_token), run_bg=run_bg, services=Services(
            make_one=engine.make_one,
            instagram_ready=ig.ready,
            run_analysis=lambda: analyst.run(cfg, db, ai),
            stats_text=lambda: analyst.stats_text(db),
            check_ai=lambda: "\n".join(ai.check()),
            models_report=lambda: ai.models_report() + "\n\n" + ai.image_models_report(),
            test_image=lambda: images.test_generate(cfg, db, ai),
            make_for=engine.make_for,
            occasions_text=engine.occasions_text,
            scout_trends=lambda: scout.run(cfg, db, ai),
        ))
        engine.bot = bot
    engine.recover()
    return engine, bot


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = Config()
    engine, bot = build(cfg)
    server.start(cfg.media_dir, cfg.port)
    log.info("старт: слоты %s, источник фото %s, Instagram %s", cfg.publish_hours_list[: cfg.reels_per_day],
             cfg.image_source, "подключён" if engine.ig.ready() else "не подключён")
    if not bot:
        log.warning("ADMIN_BOT_TOKEN не задан — работаю без админ-бота")
    elif cfg.admin_chat_id:
        gen = {"mixed": f"новые кадры примерно в {cfg.generate_share:.0%} роликов",
               "generate": "новые кадры в каждом ролике",
               "library": "только готовые фото, без генерации"}.get(cfg.image_source, cfg.image_source)
        bot.say(f"🟢 Контент-движок запущен. Фото: {gen}; лимит генерации {cfg.max_images_per_day} кадров "
                "в сутки. /help — команды.")

    def poll_loop():
        while True:
            try:
                bot.poll_once()
            except Exception as e:  # noqa: BLE001
                log.warning("poll: %s", e)
                time.sleep(5)

    if bot:
        threading.Thread(target=poll_loop, name="bot", daemon=True).start()
    while True:
        engine.tick()
        time.sleep(30)


if __name__ == "__main__":
    main()
