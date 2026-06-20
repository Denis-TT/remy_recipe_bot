"""
Обработчики команд Remy Bot.

Содержит три публичных хендлера: ``/start``, ``/menu``, ``/help``.
Все функции соответствуют сигнатуре python-telegram-bot
(``async def handler(update, context)``) и получают общие зависимости
через ``context.application.bot_data["remy"]``.

Тексты вынесены в модульные константы — так проще поддерживать
единый стиль и переиспользовать их из других хендлеров
(например, `callbacks.show_help` также использует ``HELP_TEXT``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import ContextTypes

from ..keyboards import main_menu_keyboard, menu_keyboard, welcome_start_keyboard

if TYPE_CHECKING:
    from ..bot import RemyBot
    from config import Config


logger = logging.getLogger("remy.handlers.commands")


# --------------------------------------------------------------------------- #
# Тексты
# --------------------------------------------------------------------------- #

WELCOME_TEXT: str = (
    "👋 Привет! Я Реми — твой персональный ИИ-шеф.\n"
    "Я умею превращать хаотичные кулинарные видео из YouTube Shorts, "
    "Instagram Reels, TikTok и VK в идеальные пошаговые рецепты.\n"
    "\n"
    "✨ Что я умею:\n"
    "• Извлекать скрытые тексты авторов и слушать их голос\n"
    "• Форматировать шаги в чёткую технологическую карту\n"
    "• Вытаскивать секреты и лайфхаки шеф-поваров\n"
    "• Честно оценивать КБЖУ блюда"
)


def format_tutorial_text(cfg: "Config") -> str:
    """Текст инструкции с актуальными лимитами из конфига."""
    max_sec = int(getattr(cfg, "max_video_duration_seconds", 120) or 120)
    rate_sec = int(getattr(cfg, "url_rate_limit_seconds", 180) or 180)

    if max_sec % 60 == 0:
        max_label = f"{max_sec // 60} мин"
    else:
        max_label = f"{max_sec} сек"

    if rate_sec % 60 == 0:
        rate_label = f"{rate_sec // 60} мин"
    else:
        rate_label = f"{rate_sec} сек"

    return (
        "📖 Как пользоваться Реми\n"
        "\n"
        "1️⃣ Открой YouTube Shorts, Instagram Reels, TikTok или VK Клипы\n"
        "2️⃣ Нажми «Поделиться» → «Копировать ссылку»\n"
        "3️⃣ Вставь ссылку в этот чат\n"
        "\n"
        "⚠️ Лимиты:\n"
        f"• Видео до {max_label} — полный разбор (голос + описание)\n"
        "• Длиннее — только если в описании или субтитрах достаточно текста\n"
        f"• Не чаще 1 ссылки раз в {rate_label}\n"
        "\n"
        "Нажми «🔥 Протестировать пример», чтобы увидеть Реми в деле!"
    )


MENU_TEXT: str = "📋 Меню. Выбери действие:"

HELP_TEXT: str = (
    "📚 Как пользоваться ботом:\n"
    "\n"
    "1. Отправь ссылку на рецепт с любого сайта\n"
    "   или перешли текст рецепта (ингредиенты и шаги)\n"
    "2. Бот обработает рецепт и покажет результат\n"
    "3. Нажми «✅ Сохранить», чтобы добавить в книгу рецептов\n"
    "\n"
    "Для просмотра сохранённых рецептов используй:\n"
    "• 📖 Книга рецептов — открыть Mini App\n"
    "• 📚 Сохраненные рецепты — посмотреть категории в боте"
)


# --------------------------------------------------------------------------- #
# Утилита доступа к экземпляру бота
# --------------------------------------------------------------------------- #

def _get_bot(context: ContextTypes.DEFAULT_TYPE) -> "RemyBot":
    """Достать экземпляр :class:`RemyBot`, положенный в ``bot_data``.

    Мы намеренно вызываем ``KeyError`` при отсутствии — это программная
    ошибка настройки приложения, её не надо прятать.
    """
    return context.application.bot_data["remy"]


# --------------------------------------------------------------------------- #
# /start
# --------------------------------------------------------------------------- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда ``/start`` — приветствие и Reply-клавиатура с «📋 Меню».

    Поддерживает deep link ``/start share_<recipe_id>`` — так Mini App,
    запущенный через Menu Button или inline-кнопку, инициирует шаринг
    При первом сообщении после «Поделиться» из Mini App бот проверяет
    очередь ``pending_shares`` и отправляет карточку рецепта (Menu Button
    не поддерживает ``WebApp.sendData``).

    Reply-клавиатура содержит только «📋 Меню». Точка входа в Mini App
    «📖 Книга рецептов» — Menu Button бота (BotFather) плюс inline-меню
    по ``/menu``; сознательно не дублируем её в always-visible клавиатуре.
    """
    user = update.effective_user
    if user is not None:
        logger.info("🚀 /start от user %s (@%s)", user.id, user.username or "—")

    message = update.effective_message
    if message is None:
        return

    from .messages import try_handle_pending_share

    user = update.effective_user
    if user is not None and await try_handle_pending_share(message, context, user.id):
        return

    args = list(getattr(context, "args", None) or [])
    payload = str(args[0]).strip() if args else ""
    if not payload and message.text:
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) > 1 and parts[0].startswith("/start"):
            payload = parts[1].strip()
    if payload.startswith("share_"):
        recipe_id = payload[len("share_"):]
        if await _start_share_deeplink(update, context, recipe_id):
            return

    await message.reply_text(
        WELCOME_TEXT,
        reply_markup=welcome_start_keyboard(),
    )
    await message.reply_text(
        "👇",
        reply_markup=main_menu_keyboard(),
    )


async def _start_share_deeplink(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    recipe_id: str,
) -> bool:
    """Обработать ``/start share_<recipe_id>``: отправить share-карточку рецепта.

    Returns:
        ``True``, если рецепт отправлен (приветствие не нужно);
        ``False`` — при любой проблеме (показываем обычный welcome).
    """
    # Ленивый импорт: messages импортирует commands на уровне модуля,
    # поэтому обратный импорт делаем внутри функции.
    from .messages import _recipe_belongs_to_user, _send_shared_recipe

    message = update.effective_message
    user = update.effective_user
    recipe_id = (recipe_id or "").strip()
    if message is None or user is None or not recipe_id:
        return False

    logger.info("📤 Deep link share: user %s, recipe %s", user.id, recipe_id)
    bot = _get_bot(context)
    try:
        recipe = await bot.storage.get_recipe(recipe_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Deep link share: ошибка загрузки рецепта %s: %s", recipe_id, exc)
        return False

    if recipe is None or not _recipe_belongs_to_user(recipe, user.id):
        logger.warning("⚠️ Deep link share: рецепт %s не найден/чужой для user %s", recipe_id, user.id)
        await message.reply_text("❌ Рецепт не найден.")
        return True

    try:
        await _send_shared_recipe(message.chat_id, recipe, context.bot)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Deep link share: не удалось отправить рецепт %s: %s", recipe_id, exc)
        await message.reply_text("❌ Не удалось отправить рецепт. Попробуй ещё раз.")
    return True


# --------------------------------------------------------------------------- #
# /menu
# --------------------------------------------------------------------------- #

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда ``/menu`` — inline-меню с основными действиями."""
    bot = _get_bot(context)
    user = update.effective_user
    if user is not None:
        logger.info("📋 /menu от user %s", user.id)

    message = update.effective_message
    if message is None:
        return

    await message.reply_text(
        MENU_TEXT,
        reply_markup=menu_keyboard(bot.config.webapp_url),
    )


# --------------------------------------------------------------------------- #
# /help
# --------------------------------------------------------------------------- #

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда ``/help`` — текст с инструкцией по использованию бота."""
    user = update.effective_user
    if user is not None:
        logger.info("ℹ️ /help от user %s", user.id)

    message = update.effective_message
    if message is None:
        return

    await message.reply_text(HELP_TEXT)
