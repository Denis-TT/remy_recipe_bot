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

from ..keyboards import main_menu_keyboard, menu_keyboard

if TYPE_CHECKING:
    from ..bot import RemyBot


logger = logging.getLogger("remy.handlers.commands")


# --------------------------------------------------------------------------- #
# Тексты
# --------------------------------------------------------------------------- #

WELCOME_TEXT: str = (
    "👨\u200d🍳 Привет! Я Remy — твой кулинарный помощник.\n"
    "\n"
    "Отправь мне ссылку на рецепт, и я:\n"
    "• Извлеку ингредиенты и шаги\n"
    "• Определю тип блюда и кухню\n"
    "• Рассчитаю КБЖУ\n"
    "• Сохраню в твою книгу рецептов\n"
    "\n"
    "Используй кнопку 📋 Меню для навигации!"
)

MENU_TEXT: str = "📋 Меню. Выбери действие:"

HELP_TEXT: str = (
    "📚 Как пользоваться ботом:\n"
    "\n"
    "1. Отправь ссылку на рецепт с любого сайта\n"
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

    Если у бота задан `webapp_url` (HTTPS), рядом с «📋 Меню» появится
    ещё и кнопка «📖 Книга рецептов», открывающая Mini App прямо из
    Reply-клавиатуры. При пустом/невалидном URL-е кнопка не добавляется.
    """
    bot = _get_bot(context)
    user = update.effective_user
    if user is not None:
        logger.info("🚀 /start от user %s (@%s)", user.id, user.username or "—")

    message = update.effective_message
    if message is None:
        return

    await message.reply_text(
        WELCOME_TEXT,
        reply_markup=main_menu_keyboard(bot.config.webapp_url),
    )


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
