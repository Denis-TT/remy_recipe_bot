"""
Основной класс Telegram-бота Remy.

Класс :class:`RemyBot` — единственная публичная точка сборки:
он инициализирует все зависимости (локализацию, парсер, нормализатор,
хранилище), регистрирует хендлеры в PTB-приложении и запускает polling.

Ключевые решения:

* Все хендлеры получают доступ к общим зависимостям через
  ``application.bot_data["remy"]`` — это идиоматично для PTB и
  позволяет оставить сигнатуры хендлеров стандартными
  (``async def handler(update, context)``).

* Разрешение сигналов SIGTERM/SIGINT мы отдаём внешнему `run.py`
  (`run_polling(stop_signals=None)`). Это важно, потому что в `run.py`
  уже настроен свой единый цикл завершения (healthcheck, release lock,
  логи «👋 Получен сигнал...»), и мы не хотим, чтобы PTB его
  переопределил.

* ``temp_recipes`` — простой in-process словарь с TTL 30 минут.
  Для одно-инстансного бота этого достаточно; если в будущем
  потребуется кластер — схему заменим на Redis с тем же интерфейсом.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram import Update
from telegram.error import BadRequest

from config import Config, config as default_config

from .handlers import callbacks, commands, messages
from .localization import Localization
from .normalizer import RecipeNormalizer
from .parser import InstagramParser, ParserRegistry, create_parser_registry, ensure_images_dir
from .apify_guard import ApifyDailyGuard, configure_apify_guard
from .rate_limit import UserRateLimiter
from .recipe_vault import RecipeVault, VaultFailureError, VaultPipelineResult
from .ytdlp_mixin import YtdlpWhisperMixin
from .storage import SupabaseStorage


logger = logging.getLogger("remy.bot")


# Время жизни временного рецепта в `temp_recipes`.
TEMP_RECIPE_TTL_SECONDS: int = 30 * 60


class RemyBot:
    """Основной класс бота Remy.

    Сводит воедино все модули проекта и предоставляет единственный
    публичный метод — :meth:`run`, — запускающий polling через PTB.

    Attributes:
        config: Конфигурация приложения.
        loc: Экземпляр :class:`Localization` для текущего языка (`"ru"`).
        parser: Реестр парсеров (веб и будущие — YouTube/Instagram).
        normalizer: AI-нормализатор сырого текста или изображения (vision)
            в структурированный рецепт.
        storage: Реализация :class:`BaseStorage` (по умолчанию Supabase).
        temp_recipes: Временный кэш распарсенных рецептов,
            ключ — Telegram user id.
    """

    def __init__(self, cfg: Optional[Config] = None) -> None:
        """Создать экземпляр бота.

        Args:
            cfg: Необязательная конфигурация; если не указана — берётся
                глобальный объект `config` из `config.py`. Позволяет
                подменять конфиг в тестах.
        """
        self.config: Config = cfg if cfg is not None else default_config

        self.loc: Localization = Localization("ru")
        ensure_images_dir()
        self.parser: ParserRegistry = create_parser_registry(self.config)
        self.normalizer: RecipeNormalizer = RecipeNormalizer(
            self.config.github_token,
            model=self.config.github_model,
            reasoning_effort=self.config.github_reasoning_effort,
        )
        self.storage: SupabaseStorage = SupabaseStorage(
            self.config.supabase_url,
            self.config.supabase_key,
        )
        self.recipe_vault: RecipeVault = RecipeVault(self.storage, self.config)
        self.temp_recipes: Dict[int, Dict[str, Any]] = {}
        self.processing_urls: Dict[int, str] = {}
        self.url_rate_limiter = UserRateLimiter(self.config.url_rate_limit_seconds)
        self.photo_rate_limiter = UserRateLimiter(self.config.photo_rate_limit_seconds)
        self.text_rate_limiter = UserRateLimiter(self.config.text_rate_limit_seconds)
        configure_apify_guard(ApifyDailyGuard(self.config.apify_max_runs_per_day))
        self.heavy_job_semaphore = asyncio.Semaphore(self.config.max_concurrent_video_jobs)
        for parser in self.parser.parsers:
            if isinstance(parser, YtdlpWhisperMixin):
                parser.heavy_job_semaphore = self.heavy_job_semaphore

        self._app: Optional[Application] = None

    # ------------------------------------------------------------------ #
    # Временный кэш рецептов
    # ------------------------------------------------------------------ #

    def cleanup_expired_temp_recipes(self) -> int:
        """Удалить просроченные записи из `temp_recipes`.

        Returns:
            Количество удалённых записей — полезно для логирования
            и тестов.
        """
        now = time.time()
        expired = [
            user_id
            for user_id, entry in self.temp_recipes.items()
            if now - float(entry.get("timestamp", 0)) > TEMP_RECIPE_TTL_SECONDS
        ]
        for user_id in expired:
            self.temp_recipes.pop(user_id, None)

        if expired:
            logger.info("🧹 Очищено просроченных рецептов: %d", len(expired))

        return len(expired)

    def _preload_whisper_background(self) -> None:
        """Запустить предзагрузку faster-whisper в фоне (не блокирует polling)."""
        for parser in self.parser.parsers:
            if not isinstance(parser, YtdlpWhisperMixin) or not parser._local_enabled:
                continue

            def _run_preload(vp: YtdlpWhisperMixin = parser) -> None:
                import asyncio

                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(vp.preload_whisper_model())
                except Exception as exc:  # noqa: BLE001
                    logger.warning("⚠️ Предзагрузка Whisper не удалась: %s", exc)
                finally:
                    loop.close()

            threading.Thread(
                target=_run_preload,
                name="whisper-preload",
                daemon=True,
            ).start()
            logger.info("⏳ Предзагрузка faster-whisper запущена в фоне...")
            return

    # ------------------------------------------------------------------ #
    # Запуск / остановка
    # ------------------------------------------------------------------ #

    def run(self) -> None:
        """Построить PTB-приложение, зарегистрировать хендлеры и начать polling.

        Метод блокирующий — возвращает управление только после
        корректной остановки (например, по SIGTERM из `run.py`).
        """
        logger.info("🤖 Инициализация Telegram-приложения...")

        app = Application.builder().token(self.config.telegram_token).build()

        # Делаем себя доступными для всех хендлеров.
        app.bot_data["remy"] = self

        # --- Команды ------------------------------------------------------
        app.add_handler(CommandHandler("start", commands.start))
        app.add_handler(CommandHandler("menu", commands.menu))
        app.add_handler(CommandHandler("help", commands.help_command))

        # --- Callback-кнопки --------------------------------------------
        app.add_handler(CallbackQueryHandler(callbacks.handle_callback))

        # --- Фото (vision) -----------------------------------------------
        app.add_handler(MessageHandler(filters.PHOTO, messages.handle_photo))

        # --- Команды из Telegram Mini App --------------------------------
        app.add_handler(
            MessageHandler(
                filters.StatusUpdate.WEB_APP_DATA,
                messages.handle_webapp_data,
            )
        )

        # --- Произвольный текст ------------------------------------------
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                messages.handle_text,
            )
        )

        # --- Глобальный error handler ------------------------------------
        app.add_error_handler(self._on_error)

        self._app = app
        self._preload_whisper_background()
        logger.info("🚀 Бот Remy запущен (polling)...")

        try:
            from run import mark_bot_polling_ready

            mark_bot_polling_ready()
        except ImportError:
            pass

        try:
            # stop_signals=None: сигналы обрабатываются `run.py`.
            # drop_pending_updates=True: при перезапуске на Railway
            # мы не хотим обрабатывать накопленные апдейты.
            app.run_polling(
                drop_pending_updates=True,
                stop_signals=None,
            )
        finally:
            logger.info("🛑 Polling остановлен")
            self._app = None

    def stop(self) -> None:
        """Остановить polling (вызывается извне, напр. из `run.py`).

        Метод потокобезопасен — `Application.stop_running` специально
        спроектирован так, чтобы его можно было дёрнуть из обработчика
        сигнала.
        """
        app = self._app
        if app is None:
            return

        try:
            app.stop_running()
            logger.info("🔕 Сигнал остановки передан приложению")
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️  Ошибка при вызове stop_running(): %s", exc)

    # ------------------------------------------------------------------ #
    # Error handler
    # ------------------------------------------------------------------ #

    async def _on_error(
        self,
        update: object,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Глобальный обработчик необработанных исключений в хендлерах."""
        logger.exception("❌ Исключение в хендлере: %s", context.error)

        if not isinstance(update, Update):
            return

        user_text = (
            "❌ Что-то пошло не так. Попробуй через минуту или нажми /start."
        )
        message = update.effective_message
        if message is not None:
            try:
                await message.reply_text(user_text)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Не удалось отправить сообщение об ошибке: %s", exc)

        query = update.callback_query
        if query is not None:
            try:
                await query.answer("Ошибка. Попробуй позже.", show_alert=True)
            except BadRequest:
                pass


# --------------------------------------------------------------------------- #
# Встроенные smoke-тесты
# --------------------------------------------------------------------------- #
# Проверяем только то, что не требует валидной сети и реальных токенов:
# сборку клавиатур, форматирование рецепта, TTL-очистку `temp_recipes`.
# Полная интеграция с Telegram и Supabase проверяется вручную командой
# `python run.py` с настроенным .env.

if __name__ == "__main__":
    import os
    import sys

    # Подменяем переменные окружения фиктивными значениями, чтобы
    # `config.py` не падал на импорте.
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:telegram")
    os.environ.setdefault("GITHUB_TOKEN", "test-github")
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "test-key")

    # Перезагружаем модуль конфигурации (если он уже был импортирован выше —
    # это фактически no-op, но полезно при прогоне из редактора).
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

    from importlib import reload  # noqa: E402

    import config as _cfg_module  # noqa: E402

    reload(_cfg_module)

    bot = RemyBot(_cfg_module.config)

    # --- TTL-очистка ---------------------------------------------------- #
    bot.temp_recipes[1] = {"recipe": {"title": "Актуальный"}, "timestamp": time.time()}
    bot.temp_recipes[2] = {"recipe": {"title": "Старый"}, "timestamp": 0}
    assert bot.cleanup_expired_temp_recipes() == 1
    assert 1 in bot.temp_recipes and 2 not in bot.temp_recipes

    # --- Форматирование рецепта ---------------------------------------- #
    from src.handlers.messages import (  # noqa: E402
        format_recipe,
        format_recipe_for_telegram,
    )

    demo = {
        "title": "Борщ классический",
        "description": "",
        "meal_type": "lunch",
        "dish_type": "soup",
        "main_ingredient": "beef",
        "difficulty": "medium",
        "cuisine": "russian",
        "prep_time": 20,
        "cook_time": 40,
        "total_time": 60,
        "servings": 6,
        "ingredients": [
            {"name": "Говядина", "amount": 500, "unit": "г", "notes": ""},
            {"name": "Свекла", "amount": 2, "unit": "шт", "notes": "средние"},
        ],
        "steps": [
            {"step_number": 1, "description": "Сварить бульон."},
            {"step_number": 2, "description": "Добавить овощи."},
        ],
        "nutrition_per_serving": {"calories": 350, "protein": 20, "fat": 15, "carbs": 40},
    }

    text = format_recipe(demo, bot)
    assert text == format_recipe_for_telegram(demo, bot)
    assert "Борщ классический" in text
    assert "🍲" in text
    assert "Русская" in text
    assert "Обеды" in text
    assert "Средне" in text
    assert "Супы" in text and "Говядина" in text  # dish_type + main_ingredient
    assert "500" in text and "Говядина" in text
    assert "1. Сварить бульон." in text
    assert "350" in text and "ккал" in text
    assert "Рецепт обработан ИИ" in text

    # --- Клавиатуры ---------------------------------------------------- #
    from src.keyboards import (  # noqa: E402
        dish_types_keyboard,
        main_ingredients_keyboard,
        menu_keyboard,
        recipe_detail_keyboard,
        recipes_list_keyboard,
        save_recipe_keyboard,
    )

    menu_kb = menu_keyboard("")
    assert any("📚" in btn.text for row in menu_kb.inline_keyboard for btn in row)

    menu_kb_webapp = menu_keyboard("https://example.com")
    assert any(
        getattr(btn, "web_app", None) is not None
        for row in menu_kb_webapp.inline_keyboard
        for btn in row
    )

    save_kb = save_recipe_keyboard()
    data_values = {btn.callback_data for row in save_kb.inline_keyboard for btn in row}
    assert data_values == {"save", "dont_save"}

    assert callable(messages.handle_photo)

    dish_kb = dish_types_keyboard(
        bot.loc,
        [{"key": "soup", "count": 5}, {"key": "baking", "count": 3}],
    )
    callback_values = [
        btn.callback_data for row in dish_kb.inline_keyboard for btn in row
    ]
    assert "dishtype_soup" in callback_values
    assert "dishtype_baking" in callback_values
    assert "back_to_menu" in callback_values

    ingredient_kb = main_ingredients_keyboard(
        bot.loc,
        "soup",
        [{"key": "beef", "count": 2}],
    )
    ingredient_callbacks = [
        btn.callback_data for row in ingredient_kb.inline_keyboard for btn in row
    ]
    assert "ingredient_soup_beef" in ingredient_callbacks

    recipes_kb = recipes_list_keyboard(
        [{"id": "00000000-0000-0000-0000-000000000001", "title": "Суп"}],
        "soup",
        "beef",
    )
    recipe_callbacks = [
        btn.callback_data for row in recipes_kb.inline_keyboard for btn in row
    ]
    assert "view_soup_beef_00000000-0000-0000-0000-000000000001" in recipe_callbacks
    assert "dishtype_soup" in recipe_callbacks

    detail_kb = recipe_detail_keyboard("00000000-0000-0000-0000-000000000001", "soup", "beef")
    detail_callbacks = [
        btn.callback_data for row in detail_kb.inline_keyboard for btn in row
    ]
    assert "share_00000000-0000-0000-0000-000000000001" in detail_callbacks

    print("✅ Smoke-тесты RemyBot пройдены!")
