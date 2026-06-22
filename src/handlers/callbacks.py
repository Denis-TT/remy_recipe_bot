"""
Обработчик inline-кнопок Remy Bot.

Публичная точка входа — :func:`handle_callback`, зарегистрированная
в :class:`src.bot.RemyBot` как единственный `CallbackQueryHandler`.
Внутри — диспетчер по префиксам `callback_data`:

    save                — сохранить рецепт из `temp_recipes[user_id]`
    dont_save           — скрыть кнопки под распарсенным рецептом
    show_categories     — показать список типов блюд пользователя
    show_help           — показать текст помощи
    run_example_test    — запустить эталонный пример рецепта из /start
    show_tutorial_info  — инструкция и лимиты (онбординг)
    prompt_own_link     — подсказка: вставить свою ссылку в чат
    go_to_start         — вернуться к приветствию /start
    feedback            — быстрая форма обратной связи
    dishtype_{dish_type} — показать основные ингредиенты внутри типа блюда
    ingredient_{dish_type}_{main_ingredient} — показать рецепты по паре ключей
    view_{recipe_id} или view_{dish_type}_{main_ingredient}_{recipe_id} — показать детальный рецепт
    share_{recipe_id}   — отправить рецепт в чат как share-сообщение
    delete_{recipe_id}  — удалить рецепт
    back_to_menu        — вернуться в главное inline-меню
    back_to_categories  — вернуться к списку категорий

Функции показа экранов (:func:`show_categories`, :func:`show_main_ingredients`,
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
    dish_types_keyboard,
    main_ingredients_keyboard,
    menu_keyboard,
    recipe_detail_keyboard,
    recipes_list_keyboard,
    tutorial_back_keyboard,
    welcome_start_keyboard,
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

    data = (query.data or "").strip()

    # Сначала отвечаем Telegram'у — иначе пользователь увидит «часики»
    # на кнопке. Для run_example_test ответ — в своём хендлере (с текстом).
    if data != "run_example_test":
        try:
            await query.answer()
        except BadRequest as exc:
            logger.debug("answer() для старого callback не сработал: %s", exc)

    user_id = query.from_user.id if query.from_user else 0
    logger.info("🔥 CALLBACK: %s от user %s", data, user_id)

    bot = _get_bot(context)

    # --- Онбординг /start ------------------------------------------------- #
    if data == "show_tutorial_info":
        await _safe_edit(
            query,
            commands.format_tutorial_text(bot.config),
            reply_markup=tutorial_back_keyboard(),
        )
        return

    if data == "go_to_start":
        await _safe_edit(
            query,
            commands.WELCOME_TEXT,
            reply_markup=welcome_start_keyboard(),
        )
        return

    if data == "prompt_own_link":
        await _safe_edit(
            query,
            commands.OWN_LINK_PROMPT_TEXT,
            reply_markup=tutorial_back_keyboard(),
        )
        return

    if data == "run_example_test":
        await _callback_run_example_test(query, context, bot, user_id)
        return

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
    if data.startswith("dishtype_"):
        dish_type = data[len("dishtype_"):]
        await show_main_ingredients(bot, user_id, dish_type, send=query.edit_message_text)
        return

    if data.startswith("ingredient_"):
        tail = data[len("ingredient_"):]
        parts = tail.split("_", 1)
        if len(parts) != 2:
            logger.warning("❓ Некорректный ingredient callback: %r", data)
            await show_categories(bot, user_id, send=query.edit_message_text)
            return
        dish_type, main_ingredient = parts
        await show_recipes_for_dish_ingredient(
            bot,
            user_id,
            dish_type,
            main_ingredient,
            send=query.edit_message_text,
        )
        return

    if data.startswith("view_"):
        dish_type = ""
        main_ingredient = ""
        recipe_id = data[len("view_"):]
        parts = recipe_id.split("_", 2)
        if len(parts) == 3:
            dish_type, main_ingredient, recipe_id = parts
        await show_recipe_detail(
            bot,
            user_id,
            recipe_id,
            dish_type=dish_type,
            main_ingredient=main_ingredient,
            send=query.edit_message_text,
        )
        return

    if data.startswith("share_"):
        recipe_id = data[len("share_"):]
        await _callback_share(bot, query, user_id, recipe_id, context)
        return

    if data == "chef_temp":
        await _callback_chef_temp(bot, query, user_id, context)
        return

    if data == "chef_more":
        await _callback_chef_more(bot, query, user_id)
        return

    if data == "chef_exit":
        await _callback_chef_exit(bot, query, user_id)
        return

    if data.startswith("chef_"):
        recipe_id = data[len("chef_"):]
        await _callback_chef(bot, query, user_id, recipe_id, context)
        return

    if data.startswith("delete_"):
        recipe_id = data[len("delete_"):]
        await _callback_delete(bot, query, user_id, recipe_id)
        return

    logger.warning("❓ Неизвестный callback_data: %r", data)


# --------------------------------------------------------------------------- #
# Онбординг /start
# --------------------------------------------------------------------------- #

async def _callback_run_example_test(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    bot: "RemyBot",
    user_id: int,
) -> None:
    """Запустить эталонный пример рецепта (как будто пользователь прислал ссылку)."""
    url = str(bot.config.example_test_url or "").strip()
    if not url.startswith(("http://", "https://")):
        try:
            await query.answer("Пример не настроен (EXAMPLE_TEST_URL)", show_alert=True)
        except BadRequest:
            pass
        return

    message = query.message
    if message is None:
        return

    try:
        await query.answer("Запускаю пример…")
    except BadRequest:
        pass

    logger.info("🔥 run_example_test: user %s, url %s", user_id, url)

    await message.reply_text(
        commands.format_example_simulation_text(url),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )

    from .messages import _handle_url

    await _handle_url(message, context, user_id, url, skip_rate_limit=True)


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
    """Показать первый уровень сохранённых рецептов: ``dish_type``.

    Args:
        bot: Экземпляр :class:`RemyBot`.
        user_id: Telegram ID пользователя.
        send: Функция отправки — обычно либо ``message.reply_text``,
            либо ``query.edit_message_text``. Это позволяет одному
            и тому же коду работать как для новой отправки, так и
            для редактирования существующего сообщения.
    """
    try:
        dish_types = await bot.storage.get_dish_types(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка получения типов блюд: %s", exc)
        await _invoke_send(
            send,
            f"❌ Не удалось получить список типов блюд: <code>{_html_escape_str(str(exc))}</code>",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if not dish_types:
        logger.info("📂 Показываю типы блюд: у user %s ещё ничего не сохранено", user_id)
        await _invoke_send(
            send,
            "📭 У тебя пока нет сохранённых рецептов.\n"
            "Пришли ссылку на рецепт — и он появится здесь.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    logger.info("Показаны типы блюд: %s", dish_types)
    await _invoke_send(
        send,
        "📚 Выбери тип блюда:",
        reply_markup=dish_types_keyboard(bot.loc, dish_types),
    )


async def show_main_ingredients(
    bot: "RemyBot",
    user_id: int,
    dish_type: str,
    *,
    send: SendFn,
) -> None:
    """Показать второй уровень: ``main_ingredient`` внутри ``dish_type``."""
    dish_key = str(dish_type or "main").strip()
    try:
        ingredients = await bot.storage.get_main_ingredients(user_id, dish_key)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка получения ингредиентов для %s: %s", dish_key, exc)
        await _invoke_send(
            send,
            f"❌ Не удалось получить ингредиенты: <code>{_html_escape_str(str(exc))}</code>",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    dish_name = bot.loc.get_dish_type_display(dish_key)
    if not ingredients:
        await _invoke_send(
            send,
            f"📭 В разделе {dish_name} пока пусто.",
            reply_markup=main_ingredients_keyboard(bot.loc, dish_key, []),
        )
        return

    logger.info("Показаны ингредиенты для %s: %s", dish_key, ingredients)
    await _invoke_send(
        send,
        f"{dish_name}: выбери основной ингредиент",
        reply_markup=main_ingredients_keyboard(bot.loc, dish_key, ingredients),
    )


async def show_recipes_for_dish_ingredient(
    bot: "RemyBot",
    user_id: int,
    dish_type: str,
    main_ingredient: str,
    *,
    send: SendFn,
) -> None:
    """Показать рецепты по паре ``dish_type`` / ``main_ingredient``."""
    dish_key = str(dish_type or "main").strip()
    ingredient_key = str(main_ingredient or "other").strip()
    try:
        recipes = await bot.storage.get_recipes_by_dish_and_ingredient(
            user_id,
            dish_key,
            ingredient_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "❌ Ошибка получения рецептов для %s/%s: %s",
            dish_key,
            ingredient_key,
            exc,
        )
        await _invoke_send(
            send,
            f"❌ Не удалось получить рецепты: <code>{_html_escape_str(str(exc))}</code>",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    dish_name = bot.loc.get_dish_type_display(dish_key)
    ingredient_name = bot.loc.get_main_ingredient_display(ingredient_key)
    if not recipes:
        await _invoke_send(
            send,
            f"📭 В разделе {dish_name} / {ingredient_name} пока пусто.",
            reply_markup=recipes_list_keyboard([], dish_key, ingredient_key),
        )
        return

    logger.info("Показаны рецепты для %s/%s", dish_key, ingredient_key)
    await _invoke_send(
        send,
        f"{dish_name} / {ingredient_name} — {len(recipes)} шт.",
        reply_markup=recipes_list_keyboard(recipes, dish_key, ingredient_key),
    )


async def show_recipe_detail(
    bot: "RemyBot",
    user_id: int,
    recipe_id: str,
    *,
    dish_type: str = "",
    main_ingredient: str = "",
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
        reply_markup=recipe_detail_keyboard(recipe_id, dish_type, main_ingredient),
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


async def _callback_chef_more(
    bot: "RemyBot",
    query: CallbackQuery,
    user_id: int,
) -> None:
    """Продолжить сессию шефа после ответа."""
    from .messages import _get_chef_session, _touch_chef_session

    message = query.message
    if message is None:
        return
    session = _get_chef_session(bot, user_id)
    if session is None:
        await message.reply_text(
            "Сессия шефа уже завершена. Открой рецепт и нажми «Спросить у шефа Реми».",
        )
        return
    _touch_chef_session(bot, user_id)
    title = str(session.get("title") or "рецепт").strip()
    await message.reply_text(f"👨‍🍳 Задай следующий вопрос по рецепту «{title}».")


async def _callback_chef_exit(
    bot: "RemyBot",
    query: CallbackQuery,
    user_id: int,
) -> None:
    """Выйти из режима вопросов шефу."""
    from .messages import _CHEF_EXIT_TEXT, end_chef_session

    message = query.message
    if message is None:
        return
    end_chef_session(bot, user_id)
    await message.reply_text(_CHEF_EXIT_TEXT)


async def _callback_chef(
    bot: "RemyBot",
    query: CallbackQuery,
    user_id: int,
    recipe_id: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Начать сессию вопросов шефу по сохранённому рецепту."""
    message = query.message
    if message is None:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    recipe_id = (recipe_id or "").strip()
    if not recipe_id:
        return

    try:
        recipe = await bot.storage.get_recipe(recipe_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Chef callback: загрузка %s: %s", recipe_id, exc)
        await message.reply_text("❌ Не удалось загрузить рецепт.")
        return

    if recipe is None:
        await message.reply_text("❌ Рецепт не найден.")
        return

    from .messages import start_chef_session

    await start_chef_session(message, context, user_id, recipe)


async def _callback_chef_temp(
    bot: "RemyBot",
    query: CallbackQuery,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Начать сессию шефа по рецепту из temp_recipes (до сохранения)."""
    message = query.message
    if message is None:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    bot.cleanup_expired_temp_recipes()
    entry = bot.temp_recipes.get(user_id)
    if entry is None:
        await message.reply_text(
            "⚠️ Рецепт больше недоступен. Пришли ссылку ещё раз.",
        )
        return

    from .messages import start_chef_session

    await start_chef_session(message, context, user_id, dict(entry["recipe"]))


async def _callback_share(
    bot: "RemyBot",
    query: CallbackQuery,
    user_id: int,
    recipe_id: str,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Отправить сохранённый рецепт в чат как share-сообщение."""
    logger.info("📤 Callback share: user %s, recipe %s", user_id, recipe_id)
    message = query.message
    if message is None:
        logger.warning("⚠️ Callback share %s: query.message недоступен", recipe_id)
        return
    try:
        recipe = await bot.storage.get_recipe(recipe_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка получения рецепта %s для шаринга: %s", recipe_id, exc)
        try:
            await message.reply_text("❌ Не удалось загрузить рецепт для шаринга.")
        except BadRequest:
            pass
        return

    if recipe is None:
        try:
            await message.reply_text("❌ Рецепт не найден.")
        except BadRequest:
            pass
        return

    owner = recipe.get("user_id")
    try:
        is_owner = owner is None or int(owner) == int(user_id)
    except (TypeError, ValueError):
        is_owner = False
    if not is_owner:
        logger.warning(
            "⚠️  user %s пытается поделиться чужим рецептом %s (owner=%s)",
            user_id,
            recipe_id,
            owner,
        )
        try:
            await message.reply_text("❌ Рецепт не найден.")
        except BadRequest:
            pass
        return

    from .messages import _send_shared_recipe

    try:
        await _send_shared_recipe(message.chat_id, recipe, context.bot)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Callback share %s: ошибка отправки: %s", recipe_id, exc, exc_info=True)
        try:
            await message.reply_text("❌ Не удалось отправить рецепт. Попробуй ещё раз.")
        except BadRequest:
            pass


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
