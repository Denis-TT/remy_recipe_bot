"""
Обработчик inline-кнопок Remy Bot.

Публичная точка входа — :func:`handle_callback`, зарегистрированная
в :class:`src.bot.RemyBot` как единственный `CallbackQueryHandler`.
Внутри — диспетчер по префиксам `callback_data`:

    save                — сохранить рецепт из `temp_recipes[user_id]`
    dont_save           — скрыть кнопки под распарсенным рецептом
    show_categories     — показать список категорий пользователя
    show_help           — показать текст помощи
    feedback            — быстрая форма обратной связи
    cat_{meal_type}     — показать рецепты в категории
    view_{recipe_id}    — показать детальный рецепт
    delete_{recipe_id}  — удалить рецепт
    back_to_menu        — вернуться в главное inline-меню
    back_to_categories  — вернуться к списку категорий

Функции показа экранов (:func:`show_categories`, :func:`show_recipes_in_category`,
:func:`show_recipe_detail`) спроектированы так, чтобы их могли
переиспользовать и :mod:`messages` (при переходе по кнопке Reply-клавиатуры),
и сам callback-хендлер (при нажатии inline-кнопки). Абстракция —
callable ``send``, куда передаются ``text`` и ``reply_markup``;
для сообщения это ``message.reply_text``, для callback-query —
``query.edit_message_text``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from telegram import CallbackQuery, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..keyboards import (
    back_to_menu_keyboard,
    categories_keyboard,
    menu_keyboard,
    recipe_detail_keyboard,
    recipes_list_keyboard,
)
from . import commands

if TYPE_CHECKING:
    from ..bot import RemyBot


logger = logging.getLogger("remy.handlers.callbacks")


# Тип «посыльного» — унифицированная функция отправки/редактирования
# сообщения. Возвращает что угодно (нам не важно).
SendFn = Callable[..., Awaitable[Any]]


# --------------------------------------------------------------------------- #
# Главный диспетчер
# --------------------------------------------------------------------------- #

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Принять callback_query и маршрутизировать по `callback_data`."""
    query = update.callback_query
    if query is None:
        return

    # Сначала отвечаем Telegram'у — иначе пользователь увидит «часики»
    # на кнопке. Если выкинется BadRequest (старый callback), молча
    # идём дальше.
    try:
        await query.answer()
    except BadRequest as exc:
        logger.debug("answer() для старого callback не сработал: %s", exc)

    data = (query.data or "").strip()
    user_id = query.from_user.id if query.from_user else 0
    logger.info("🔥 CALLBACK: %s от user %s", data, user_id)

    bot = _get_bot(context)

    # --- Сохранение распарсенного рецепта --------------------------------- #
    if data == "save":
        await _callback_save(query, bot, user_id)
        return

    if data == "dont_save":
        await _callback_dont_save(query, bot, user_id)
        return

    # --- Навигация по меню ------------------------------------------------- #
    if data == "show_categories":
        await show_categories(bot, user_id, send=query.edit_message_text)
        return

    if data == "show_help":
        await _safe_edit(
            query,
            commands.HELP_TEXT,
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "feedback":
        await _safe_edit(
            query,
            "✉️ Обратная связь: напиши автору бота в Telegram @remy_feedback — "
            "любой отзыв поможет сделать Remy лучше!",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "back_to_menu":
        await _safe_edit(
            query,
            commands.MENU_TEXT,
            reply_markup=menu_keyboard(bot.config.webapp_url),
        )
        return

    if data == "back_to_categories":
        await show_categories(bot, user_id, send=query.edit_message_text)
        return

    # --- Экраны категорий/рецептов ---------------------------------------- #
    if data.startswith("cat_"):
        meal_type = data[len("cat_"):]
        await show_recipes_in_category(bot, user_id, meal_type, send=query.edit_message_text)
        return

    if data.startswith("view_"):
        recipe_id = data[len("view_"):]
        await show_recipe_detail(bot, user_id, recipe_id, send=query.edit_message_text)
        return

    if data.startswith("delete_"):
        recipe_id = data[len("delete_"):]
        await _callback_delete(bot, query, user_id, recipe_id)
        return

    logger.warning("❓ Неизвестный callback_data: %r", data)


# --------------------------------------------------------------------------- #
# Save / Don't save
# --------------------------------------------------------------------------- #

async def _callback_save(query: CallbackQuery, bot: "RemyBot", user_id: int) -> None:
    """Сохранить рецепт из временного кэша в Supabase."""
    bot.cleanup_expired_temp_recipes()

    entry = bot.temp_recipes.pop(user_id, None)
    if entry is None:
        logger.warning("⚠️  Нет временного рецепта для user %s", user_id)
        await _safe_edit(
            query,
            "⚠️ Рецепт больше недоступен. Пришли ссылку ещё раз.",
        )
        return

    recipe = dict(entry["recipe"])
    img_url = str(recipe.get("image_url") or "").strip()
    if not img_url.startswith(("http://", "https://")):
        recipe["image_url"] = ""
    recipe.pop("image_path", None)
    try:
        saved = await bot.storage.save_recipe(user_id, recipe)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка сохранения рецепта: %s", exc)
        # Возвращаем рецепт в кэш, чтобы можно было повторить попытку.
        bot.temp_recipes[user_id] = entry
        await _safe_edit(
            query,
            f"❌ Не удалось сохранить рецепт: <code>{_html_escape_str(str(exc))}</code>",
        )
        return

    title = saved.get("title") or recipe.get("title") or "рецепт"
    logger.info("💾 Рецепт «%s» сохранён (user %s)", title, user_id)

    # Убираем кнопки — в заголовке ставим отметку «сохранено».
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest:
        pass

    message = query.message
    if message is not None:
        await message.reply_text(f"✅ Сохранено: «{title}»")


async def _callback_dont_save(query: CallbackQuery, bot: "RemyBot", user_id: int) -> None:
    """Отменить сохранение: снять кнопки, стереть из временного кэша."""
    bot.temp_recipes.pop(user_id, None)
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except BadRequest as exc:
        logger.debug("edit_message_reply_markup: %s", exc)

    message = query.message
    if message is not None:
        try:
            await message.reply_text("🗑 Хорошо, не сохраняю.")
        except BadRequest:
            pass


# --------------------------------------------------------------------------- #
# Показ категорий / списка рецептов / детального рецепта
# --------------------------------------------------------------------------- #

async def show_categories(bot: "RemyBot", user_id: int, *, send: SendFn) -> None:
    """Показать список категорий пользователя.

    Args:
        bot: Экземпляр :class:`RemyBot`.
        user_id: Telegram ID пользователя.
        send: Функция отправки — обычно либо ``message.reply_text``,
            либо ``query.edit_message_text``. Это позволяет одному
            и тому же коду работать как для новой отправки, так и
            для редактирования существующего сообщения.
    """
    try:
        categories = await bot.storage.get_categories(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка получения категорий: %s", exc)
        await _invoke_send(
            send,
            f"❌ Не удалось получить список категорий: <code>{_html_escape_str(str(exc))}</code>",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if not categories:
        logger.info("📂 Показываю категории: у user %s ещё ничего не сохранено", user_id)
        await _invoke_send(
            send,
            "📭 У тебя пока нет сохранённых рецептов.\n"
            "Пришли ссылку на рецепт — и он появится здесь.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    logger.info("📂 Показываю категории: %d категории", len(categories))
    await _invoke_send(
        send,
        "📚 Твои категории:",
        reply_markup=categories_keyboard(bot.loc, categories),
    )


async def show_recipes_in_category(
    bot: "RemyBot",
    user_id: int,
    meal_type: str,
    *,
    send: SendFn,
) -> None:
    """Показать список рецептов пользователя в указанной категории."""
    try:
        recipes = await bot.storage.get_user_recipes(user_id, meal_type=meal_type)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка получения рецептов категории %s: %s", meal_type, exc)
        await _invoke_send(
            send,
            f"❌ Не удалось получить рецепты: <code>{_html_escape_str(str(exc))}</code>",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    category_name = bot.loc.get_meal_type_display(meal_type)
    logger.info(
        "📖 Категория %s: %d рецептов (user %s)",
        meal_type,
        len(recipes),
        user_id,
    )

    if not recipes:
        await _invoke_send(
            send,
            f"📭 В категории {category_name} пока пусто.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await _invoke_send(
        send,
        f"{category_name} — {len(recipes)} шт.",
        reply_markup=recipes_list_keyboard(recipes),
    )


async def show_recipe_detail(
    bot: "RemyBot",
    user_id: int,
    recipe_id: str,
    *,
    send: SendFn,
) -> None:
    """Показать детальный рецепт с кнопками «🗑 Удалить» / «◀️ Назад»."""
    try:
        recipe = await bot.storage.get_recipe(recipe_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка получения рецепта %s: %s", recipe_id, exc)
        await _invoke_send(
            send,
            f"❌ Не удалось получить рецепт: <code>{_html_escape_str(str(exc))}</code>",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if recipe is None:
        await _invoke_send(
            send,
            "❌ Рецепт не найден.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # Защита от чужих рецептов (на всякий случай — RLS в Supabase
    # должен уже отфильтровать, но дополнительная проверка не помешает).
    owner = recipe.get("user_id")
    if owner is not None and int(owner) != int(user_id):
        logger.warning(
            "⚠️  user %s пытается открыть чужой рецепт %s (owner=%s)",
            user_id,
            recipe_id,
            owner,
        )
        await _invoke_send(
            send,
            "❌ Рецепт не найден.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    # Переиспользуем форматер из messages.py (ленивый импорт во избежание
    # циклического импорта: messages → callbacks → messages).
    from .messages import format_recipe

    text = format_recipe(recipe, bot)
    logger.info("🍽 Показан рецепт %s пользователю %s", recipe_id, user_id)

    await _invoke_send(
        send,
        text,
        reply_markup=recipe_detail_keyboard(recipe_id),
    )


async def _callback_delete(
    bot: "RemyBot",
    query: CallbackQuery,
    user_id: int,
    recipe_id: str,
) -> None:
    """Удалить рецепт пользователя и вернуть его к списку категорий."""
    try:
        deleted = await bot.storage.delete_recipe(recipe_id, user_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка удаления рецепта %s: %s", recipe_id, exc)
        await _safe_edit(
            query,
            f"❌ Не удалось удалить рецепт: <code>{_html_escape_str(str(exc))}</code>",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if not deleted:
        logger.info("🗑 Рецепт %s не найден для удаления (user %s)", recipe_id, user_id)
        await _safe_edit(
            query,
            "❌ Рецепт уже удалён или не найден.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    logger.info("🗑 Рецепт %s удалён (user %s)", recipe_id, user_id)
    await show_categories(bot, user_id, send=query.edit_message_text)


# --------------------------------------------------------------------------- #
# Совместимый API для messages.py
# --------------------------------------------------------------------------- #

async def show_categories_for_message(
    message: Message,
    bot: "RemyBot",
    user_id: int,
) -> None:
    """Тонкая обёртка — `show_categories` с отправкой новым сообщением.

    Используется, когда пользователь нажимает текстовую кнопку
    «📚 Сохраненные рецепты» на Reply-клавиатуре: там нет callback_query,
    нужно отправить новое сообщение.
    """
    await show_categories(bot, user_id, send=message.reply_text)


# --------------------------------------------------------------------------- #
# Внутренние утилиты
# --------------------------------------------------------------------------- #

def _get_bot(context: ContextTypes.DEFAULT_TYPE) -> "RemyBot":
    """Достать :class:`RemyBot` из ``bot_data``."""
    return context.application.bot_data["remy"]


async def _invoke_send(
    send: SendFn,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Вызвать `send(...)` с HTML-разметкой и типичными опциями."""
    try:
        await send(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        # edit_message_text бросает BadRequest, если текст и разметка
        # не изменились. Это не ошибка — просто логируем и идём дальше.
        if "not modified" in str(exc).lower():
            logger.debug("сообщение уже в нужном виде: %s", exc)
            return
        logger.warning("⚠️  BadRequest при отправке: %s", exc)


async def _safe_edit(
    query: CallbackQuery,
    text: str,
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Попробовать отредактировать сообщение callback'а, проглотив BadRequest."""
    try:
        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        logger.warning("⚠️  Не удалось отредактировать callback-сообщение: %s", exc)


def _html_escape_str(value: str) -> str:
    """Экранировать строку для вставки в HTML-сообщение."""
    from html import escape

    return escape(value)
