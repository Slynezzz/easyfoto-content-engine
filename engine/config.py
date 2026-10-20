"""Настройки из переменных окружения (на Railway — вкладка Variables)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _env(name: str, default: str = "") -> str:
    # Частая ошибка при вставке в Railway — кавычки или пробелы вокруг значения.
    return os.environ.get(name, default).strip().strip('"').strip("'").strip()


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


@dataclass
class Config:
    # --- хранилище (на Railway подключить Volume и указать DATA_DIR=/data)
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(ROOT / "data"))))

    # --- нейросети
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    # Свой адрес API — если ключи выданы сервисом-посредником (прокси), а не Google/OpenAI напрямую.
    gemini_base_url: str = field(default_factory=lambda: _env(
        "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/"))
    openai_base_url: str = field(default_factory=lambda: _env(
        "OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"))
    # Формат запросов к Gemini-посреднику: auto (определить самому), native (как у Google), openai
    gemini_api_style: str = field(default_factory=lambda: _env("GEMINI_API_STYLE", "auto"))
    gemini_text_model: str = field(default_factory=lambda: _env("GEMINI_TEXT_MODEL", "gemini-2.5-flash"))
    gemini_image_model: str = field(default_factory=lambda: _env("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_text_model: str = field(default_factory=lambda: _env("OPENAI_TEXT_MODEL", "gpt-4.1-mini"))
    openai_image_model: str = field(default_factory=lambda: _env("OPENAI_IMAGE_MODEL", "gpt-image-1.5"))
    # Отдельный ключ только для картинок (например, AI STAR для gpt-image, а тексты — через SaintStore)
    image_api_base_url: str = field(default_factory=lambda: _env("IMAGE_API_BASE_URL").rstrip("/"))
    image_api_key: str = field(default_factory=lambda: _env("IMAGE_API_KEY"))
    image_model: str = field(default_factory=lambda: _env("IMAGE_MODEL"))

    # --- поиск для разведки трендов (ключ Google Cloud и id поисковика Programmable Search Engine)
    google_api_key: str = field(default_factory=lambda: _env("GOOGLE_API_KEY"))
    google_cse_cx: str = field(default_factory=lambda: _env("GOOGLE_CSE_CX"))
    google_research_model: str = field(default_factory=lambda: _env("GOOGLE_RESEARCH_MODEL", "gemini-2.5-flash"))
    # Отдельный сервис поиска: tavily | serper | brave | google_cse | perplexity (модель, которая ищет сама)
    search_provider: str = field(default_factory=lambda: _env("SEARCH_PROVIDER"))
    search_api_key: str = field(default_factory=lambda: _env("SEARCH_API_KEY"))
    search_base_url: str = field(default_factory=lambda: _env("SEARCH_BASE_URL").rstrip("/"))
    search_model: str = field(default_factory=lambda: _env("SEARCH_MODEL", "sonar"))
    # Если поиск не настроен или упал — всё равно предложить идеи хуков (с честной пометкой «без поиска»)
    trends_without_search: bool = field(default_factory=lambda: _env("TRENDS_WITHOUT_SEARCH", "1") not in ("0", "false", "no"))

    # --- откуда брать фото для роликов: library (готовые), generate (новые), mixed
    image_source: str = field(default_factory=lambda: _env("IMAGE_SOURCE", "mixed"))
    # В режиме mixed: доля роликов с новыми кадрами (остальные — из библиотеки)
    generate_share: float = field(default_factory=lambda: _float("GENERATE_SHARE", 0.7))
    # Сколько новых кадров генерировать на ролик (каждый кадр тратит токены посредника)
    generate_photos: int = field(default_factory=lambda: _int("GENERATE_PHOTOS", 4))
    # Предохранитель: не больше стольких сгенерированных кадров в сутки (токены посредника платные)
    max_images_per_day: int = field(default_factory=lambda: _int("MAX_IMAGES_PER_DAY", 16))
    # Сколько роликов в день отдавать под праздники и инфоповоды, когда они есть
    occasions_per_day: int = field(default_factory=lambda: _int("OCCASIONS_PER_DAY", 1))
    persona_gender: str = field(default_factory=lambda: _env("PERSONA_GENDER", "male"))  # male | female
    bot_username: str = field(default_factory=lambda: _env("BOT_USERNAME", "Easyfotobot"))

    # --- админ-бот одобрения (отдельный бот от @BotFather, НЕ токен Easyfotobot)
    admin_bot_token: str = field(default_factory=lambda: _env("ADMIN_BOT_TOKEN"))
    admin_chat_id: int = field(default_factory=lambda: _int("ADMIN_CHAT_ID", 0))

    # --- Instagram (API with Instagram Login)
    ig_user_id: str = field(default_factory=lambda: _env("IG_USER_ID"))
    ig_access_token: str = field(default_factory=lambda: _env("IG_ACCESS_TOKEN"))
    ig_graph_version: str = field(default_factory=lambda: _env("IG_GRAPH_VERSION", "v23.0"))

    # --- публичная раздача видео (Instagram скачивает ролик по ссылке)
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/"))
    port: int = field(default_factory=lambda: _int("PORT", 8080))

    # --- расписание (часовой пояс аудитории)
    timezone: str = field(default_factory=lambda: _env("TZ_NAME", "Europe/Moscow"))
    reels_per_day: int = field(default_factory=lambda: _int("REELS_PER_DAY", 2))
    publish_hours: str = field(default_factory=lambda: _env("PUBLISH_HOURS", "12,19"))
    plan_hour: int = field(default_factory=lambda: _int("PLAN_HOUR", 8))
    explore_share: float = field(default_factory=lambda: _float("EXPLORE_SHARE", 0.3))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "engine.db"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def playbook_path(self) -> Path:
        return self.data_dir / "playbook.json"

    @property
    def publish_hours_list(self) -> list[int]:
        return sorted({int(h) for h in self.publish_hours.split(",") if h.strip()})

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.media_dir, self.assets_dir,
                  self.assets_dir / "library", self.assets_dir / "before", self.assets_dir / "refs"):
            d.mkdir(parents=True, exist_ok=True)
