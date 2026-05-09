"""
Модуль конфигурации Remy Bot.

Загружает переменные окружения из файла .env (для локальной разработки)
или из окружения процесса (для Railway и других облачных платформ),
валидирует их и предоставляет приложению через глобальный объект `config`.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

# Загружаем .env, если он есть. На Railway переменные придут из окружения —
# load_dotenv просто ничего не сделает, это не ошибка.
load_dotenv()


# Допустимые значения для ENVIRONMENT
_ALLOWED_ENVIRONMENTS = {"production", "development"}

# Допустимые уровни логирования (из стандартного модуля logging)
_ALLOWED_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


@dataclass(frozen=True)
class Config:
    """Конфигурация приложения Remy Bot.

    Поля, помеченные как обязательные, должны быть заданы в окружении,
    иначе приложение завершится с ошибкой на старте.
    """

    telegram_token: str
    github_token: str
    supabase_url: str
    supabase_key: str
    log_level: str = "INFO"
    webapp_url: str = ""
    environment: str = "production"
    # YouTube Data API v3: заголовок и описание (для YouTube URL без ключа parse() не выполнить).
    youtube_api_key: str = ""
    # Apify API token: субтитры YouTube через Actor ``compass~youtube-transcript-scraper``.
    apify_api_token: str = ""
    # Устарело: ранее yt-dlp; поле оставлено для совместимости существующих .env.
    youtube_cookie_file: str = ""

    @property
    def is_development(self) -> bool:
        """Возвращает True, если приложение работает в dev-окружении."""
        return self.environment == "development"

    @property
    def is_production(self) -> bool:
        """Возвращает True, если приложение работает в prod-окружении."""
        return self.environment == "production"

    @classmethod
    def from_env(cls) -> "Config":
        """Построить конфиг из переменных окружения с валидацией.

        При отсутствии обязательных переменных печатает понятную ошибку
        в stderr и завершает процесс с кодом 1.
        """
        missing: List[str] = []

        telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not telegram_token:
            missing.append("TELEGRAM_BOT_TOKEN")

        github_token = os.getenv("GITHUB_TOKEN", "").strip()
        if not github_token:
            missing.append("GITHUB_TOKEN")

        supabase_url = os.getenv("SUPABASE_URL", "").strip()
        if not supabase_url:
            missing.append("SUPABASE_URL")

        supabase_key = os.getenv("SUPABASE_KEY", "").strip()
        if not supabase_key:
            missing.append("SUPABASE_KEY")

        if missing:
            cls._fail_missing(missing)

        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        if log_level not in _ALLOWED_LOG_LEVELS:
            print(
                f"⚠️  Неизвестный LOG_LEVEL='{log_level}', используем INFO. "
                f"Допустимые значения: {sorted(_ALLOWED_LOG_LEVELS)}",
                file=sys.stderr,
            )
            log_level = "INFO"

        webapp_url = os.getenv("WEBAPP_URL", "").strip()

        youtube_api_key = os.getenv("YOUTUBE_API_KEY", "").strip()

        apify_api_token = os.getenv("APIFY_API_TOKEN", "").strip()

        youtube_cookie_file = os.getenv("YOUTUBE_COOKIE_FILE", "").strip()

        environment = os.getenv("ENVIRONMENT", "production").strip().lower() or "production"
        if environment not in _ALLOWED_ENVIRONMENTS:
            print(
                f"⚠️  Неизвестный ENVIRONMENT='{environment}', используем 'production'. "
                f"Допустимые значения: {sorted(_ALLOWED_ENVIRONMENTS)}",
                file=sys.stderr,
            )
            environment = "production"

        return cls(
            telegram_token=telegram_token,
            github_token=github_token,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            log_level=log_level,
            webapp_url=webapp_url,
            environment=environment,
            youtube_api_key=youtube_api_key,
            apify_api_token=apify_api_token,
            youtube_cookie_file=youtube_cookie_file,
        )

    @staticmethod
    def _fail_missing(missing: List[str]) -> None:
        """Вывести понятную ошибку об отсутствующих переменных и выйти.

        Сообщение печатается в stderr, потому что на этом этапе
        логирование ещё не настроено.
        """
        print(
            "❌ Не заданы обязательные переменные окружения:\n"
            + "\n".join(f"   • {name}" for name in missing)
            + "\n\n"
            "Подсказка: создайте файл .env на основе .env.example\n"
            "           или задайте переменные в настройках Railway.",
            file=sys.stderr,
        )
        sys.exit(1)


# Глобальный объект конфигурации.
# Импортируется по всему проекту как: `from config import config`.
config: Config = Config.from_env()
