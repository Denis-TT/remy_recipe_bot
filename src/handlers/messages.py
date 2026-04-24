"""
Обработчик текстовых сообщений Remy Bot.

Маршрутизирует любое входящее текстовое сообщение:

* Нажатия на кнопки Reply-клавиатуры («📋 Меню», «📚 Сохраненные рецепты»,
  «ℹ️ Помощь») — открывают соответствующий экран;
* URL (http/https) — запускает цепочку «парсинг → нормализация →
  отображение с кнопками Сохранить/Не сохранять»;
* Любой другой текст — короткая подсказка отправить ссылку.

Дополнительно здесь живёт функция :func:`format_recipe`, которая
собирает HTML-представление распарсенного рецепта.
"""

from __future__ import annotations

import logging
import re
import time
from html import escape as _html_escape
from typing import TYPE_CHECKING, Any, List, Mapping

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..keyboards import (
    BTN_HELP,
    BTN_MENU,
    BTN_SAVED_RECIPES,
    main_menu_keyboard,
    save_recipe_keyboard,
)
from . import callbacks, commands

if TYPE_CHECKING:
    from ..bot import RemyBot


logger = logging.getLogger("remy.handlers.messages")


# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

# Максимальная длина одного Telegram-сообщения с учётом HTML-тегов.
_TG_MESSAGE_LIMIT: int = 4096

# Регэксп для поиска URL в произвольном сообщении (берём первый http/https).
# Допускаем любой непробельный суффикс — валидацию реальной доступности
# выполняет парсер; лучше ошибиться в сторону «попробовали и узнали».
_URL_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)

# Подсказка, если пользователь прислал что-то непонятное.
_NOT_A_URL_HINT: str = (
    "🤔 Не вижу ссылки. Отправь URL рецепта (начинается с http:// или https://),\n"
    "или нажми 📋 Меню, чтобы увидеть доступные действия."
)


# --------------------------------------------------------------------------- #
# Основной хендлер
# --------------------------------------------------------------------------- #

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Маршрутизировать текстовое сообщение."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return

    raw_text: str = (message.text or "").strip()
    logger.info(
        "📨 Получено сообщение от user %s: %r",
        user.id,
        raw_text[:50],
    )

    # Короткие маршруты по кнопкам Reply-клавиатуры.
    if raw_text == BTN_MENU:
        await message.reply_text(
            commands.MENU_TEXT,
            reply_markup=_menu_markup(context),
        )
        return

    if raw_text == BTN_SAVED_RECIPES:
        bot = _get_bot(context)
        await callbacks.show_categories_for_message(message, bot, user.id)
        return

    if raw_text == BTN_HELP:
        await message.reply_text(commands.HELP_TEXT)
        return

    # URL → запускаем пайплайн обработки рецепта.
    url_match = _URL_REGEX.search(raw_text)
    if url_match is not None:
        await _handle_url(message, context, user.id, url_match.group(0))
        return

    # Всё остальное — подсказка.
    bot = _get_bot(context)
    await message.reply_text(
        _NOT_A_URL_HINT,
        reply_markup=main_menu_keyboard(bot.config.webapp_url),
    )


# --------------------------------------------------------------------------- #
# URL-пайплайн
# --------------------------------------------------------------------------- #

async def _handle_url(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    url: str,
) -> None:
    """Распарсить URL, нормализовать рецепт и отправить результат."""
    bot = _get_bot(context)

    logger.info("🔍 Начинаю обработку URL: %s", url)

    status: Message = await message.reply_text("🔍 Читаю страницу...")

    # 1) Парсинг
    try:
        raw_text = await bot.parser.parse(url)
    except Exception as exc:  # noqa: BLE001 — логируем любую причину
        logger.error("❌ Ошибка обработки URL: %s", exc)
        await _safe_edit(status, f"❌ Не удалось прочитать страницу:\n<code>{_html_escape(str(exc))}</code>")
        return

    if not raw_text or not raw_text.strip():
        logger.warning("⚠️  Пустой текст после парсинга: %s", url)
        await _safe_edit(status, "❌ Со страницы не удалось извлечь текст")
        return

    # 2) Нормализация
    await _safe_edit(status, "🤖 Анализирую рецепт...")

    try:
        recipe = await bot.normalizer.normalize(raw_text)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка нормализации: %s", exc)
        await _safe_edit(status, f"❌ Не удалось обработать рецепт:\n<code>{_html_escape(str(exc))}</code>")
        return

    # 3) Валидация: нужны хотя бы title + ingredients
    title_ok = bool((recipe.get("title") or "").strip())
    ingredients_ok = bool(recipe.get("ingredients"))
    if not (title_ok and ingredients_ok):
        logger.warning(
            "⚠️  Рецепт не прошёл валидацию (title=%s, ingredients=%s)",
            title_ok,
            ingredients_ok,
        )
        await _safe_edit(
            status,
            "❌ Не удалось распознать рецепт на этой странице.\n"
            "Попробуй другую ссылку.",
        )
        return

    # 4) Сохраняем во временный кэш и показываем пользователю
    recipe["source_url"] = url
    bot.temp_recipes[user_id] = {"recipe": recipe, "timestamp": time.time()}
    bot.cleanup_expired_temp_recipes()

    logger.info(
        "✅ Рецепт обработан: «%s», meal_type=%s",
        recipe.get("title"),
        recipe.get("meal_type"),
    )

    formatted = format_recipe(recipe, bot)

    try:
        await status.edit_text(
            formatted,
            reply_markup=save_recipe_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        # Если форматированный рецепт слишком длинный или содержит «плохие»
        # HTML-последовательности — подстрахуемся чистым текстом.
        logger.warning("⚠️  Ошибка edit_text с HTML: %s — отправляю без разметки", exc)
        plain = re.sub(r"<[^>]+>", "", formatted)
        await status.edit_text(
            plain[:_TG_MESSAGE_LIMIT],
            reply_markup=save_recipe_keyboard(),
            disable_web_page_preview=True,
        )


# --------------------------------------------------------------------------- #
# Форматирование рецепта
# --------------------------------------------------------------------------- #

def format_recipe(recipe: Mapping[str, Any], bot: "RemyBot") -> str:
    """Собрать HTML-представление распарсенного рецепта.

    Args:
        recipe: Словарь с нормализованными полями (см. `RecipeNormalizer`).
        bot: Экземпляр `RemyBot` — используется только для доступа
            к `bot.loc` (локализация).

    Returns:
        Строка, готовая к отправке Telegram с `parse_mode=HTML`.
        Длина гарантированно не превышает 4096 символов.
    """
    loc = bot.loc

    meal_type = recipe.get("meal_type") or "other"
    difficulty = recipe.get("difficulty") or "medium"
    cuisine = recipe.get("cuisine") or "other"

    title = _html_escape(str(recipe.get("title") or "Без названия").strip())
    meal_emoji = loc.get_meal_type_emoji(meal_type)
    meal_name = _html_escape(loc.get_meal_type_name(meal_type))
    cuisine_name = _html_escape(loc.get_cuisine_name(cuisine))
    difficulty_display = _html_escape(loc.get_difficulty_display(difficulty))

    lines: List[str] = [
        f"{meal_emoji} <b>{title}</b>",
        "",
        f"🍽 {cuisine_name} | 📋 {meal_name} | {difficulty_display}",
    ]

    # --- Время + порции -----------------------------------------------------
    prep = _int(recipe.get("prep_time"))
    cook = _int(recipe.get("cook_time"))
    total = _int(recipe.get("total_time"))
    if prep or cook or total:
        lines.extend([
            "",
            f"⏰ Время: подготовка {prep} мин, готовка {cook} мин, всего {total} мин",
        ])

    servings = _int(recipe.get("servings"))
    if servings:
        lines.append(f"👥 Порций: {servings}")

    # --- КБЖУ ---------------------------------------------------------------
    nutrition = recipe.get("nutrition_per_serving") or {}
    if isinstance(nutrition, Mapping):
        cal = _int(nutrition.get("calories"))
        protein = _int(nutrition.get("protein"))
        fat = _int(nutrition.get("fat"))
        carbs = _int(nutrition.get("carbs"))
        if cal or protein or fat or carbs:
            lines.extend([
                "",
                "📊 КБЖУ на порцию:",
                f"🔥 {cal} ккал | 💪 {protein} г | 🧈 {fat} г | 🍚 {carbs} г",
            ])

    # --- Ингредиенты --------------------------------------------------------
    ingredients = recipe.get("ingredients") or []
    if ingredients:
        lines.extend(["", "🛒 Ингредиенты:"])
        for ing in ingredients:
            lines.append("• " + _html_escape(_format_ingredient(ing)))

    # --- Шаги ---------------------------------------------------------------
    steps = recipe.get("steps") or []
    if steps:
        lines.extend(["", "📝 Приготовление:"])
        for step in steps:
            num = _int(step.get("step_number")) if isinstance(step, Mapping) else 0
            desc = str(step.get("description") or "").strip() if isinstance(step, Mapping) else str(step)
            if not desc:
                continue
            prefix = f"{num}. " if num else "• "
            lines.append(prefix + _html_escape(desc))

    text = "\n".join(lines)
    if len(text) > _TG_MESSAGE_LIMIT:
        # Не режем по байтам грубо, чтобы не сломать HTML-тег.
        text = text[: _TG_MESSAGE_LIMIT - 40].rstrip() + "\n…\n<i>(сокращено)</i>"
    return text


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #

def _format_ingredient(raw: Any) -> str:
    """Сформировать строку «amount unit name (notes)» для одного ингредиента."""
    if not isinstance(raw, Mapping):
        return str(raw).strip()

    name = str(raw.get("name") or "").strip()
    unit = str(raw.get("unit") or "").strip()
    notes = str(raw.get("notes") or "").strip()

    amount_raw = raw.get("amount")
    amount_str = ""
    if isinstance(amount_raw, (int, float)) and amount_raw:
        amount_str = f"{int(amount_raw)}" if float(amount_raw).is_integer() else f"{amount_raw:g}"

    parts: List[str] = []
    if amount_str:
        parts.append(amount_str)
    if unit:
        parts.append(unit)
    if name:
        parts.append(name)

    line = " ".join(parts) if parts else "—"
    if notes:
        line += f" ({notes})"
    return line


def _int(value: Any) -> int:
    """Безопасно привести значение к `int` (мусор → 0)."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (ValueError, OverflowError):
            return 0
    try:
        return int(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return 0


def _menu_markup(context: ContextTypes.DEFAULT_TYPE):
    """Отложенный импорт, чтобы не тянуть циклы."""
    from ..keyboards import menu_keyboard

    bot = _get_bot(context)
    return menu_keyboard(bot.config.webapp_url)


def _get_bot(context: ContextTypes.DEFAULT_TYPE) -> "RemyBot":
    """Достать :class:`RemyBot` из ``bot_data``."""
    return context.application.bot_data["remy"]


async def _safe_edit(message: Message, text: str, **kwargs: Any) -> None:
    """Попробовать отредактировать сообщение, проглотив BadRequest.

    При редактировании статусных сообщений (`status = await reply_text(...)`)
    Telegram периодически отдаёт BadRequest (например, "message is not
    modified"); падать из-за этого в async-пайплайне незачем.
    """
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, **kwargs)
    except BadRequest as exc:
        logger.warning("⚠️  Не удалось отредактировать сообщение: %s", exc)
