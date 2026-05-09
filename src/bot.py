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

import logging
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

from config import Config, config as default_config

from .handlers import callbacks, commands, messages
from .localization import Localization
from .normalizer import RecipeNormalizer
from .parser import ParserRegistry, create_parser_registry
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
        self.parser: ParserRegistry = create_parser_registry(self.config)
        self.normalizer: RecipeNormalizer = RecipeNormalizer(self.config.github_token)
        self.storage: SupabaseStorage = SupabaseStorage(
            self.config.supabase_url,
            self.config.supabase_key,
        )
        self.temp_recipes: Dict[int, Dict[str, Any]] = {}

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
        logger.info("🚀 Бот Remy запущен (polling)...")

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
        categories_keyboard,
        menu_keyboard,
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

    cats_kb = categories_keyboard(
        bot.loc,
        [{"key": "lunch", "count": 5}, {"key": "dessert", "count": 3}],
    )
    callback_values = [
        btn.callback_data for row in cats_kb.inline_keyboard for btn in row
    ]
    assert "cat_lunch" in callback_values
    assert "cat_dessert" in callback_values
    assert "back_to_menu" in callback_values

    print("✅ Smoke-тесты RemyBot пройдены!")
