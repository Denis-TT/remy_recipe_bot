"""
Обработчик текстовых сообщений Remy Bot.

Маршрутизирует входящие сообщения:

* Нажатия на кнопки Reply-клавиатуры («📋 Меню», «📚 Сохраненные рецепты»,
  «ℹ️ Помощь») — открывают соответствующий экран;
* Фото — анализ через GitHub Models (vision), нормализация, карточка с кнопками;
* URL (http/https) — запускает цепочку «парсинг → нормализация →
  отображение с кнопками Сохранить/Не сохранять»;
* Любой другой текст — короткая подсказка отправить ссылку.

Дополнительно: :func:`format_recipe` и псевдоним :func:`format_recipe_for_telegram`
(одна реализация) — HTML-карточка рецепта для Telegram.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from html import escape as _html_escape
from io import BytesIO
from typing import TYPE_CHECKING, Any, List, Mapping, Optional

from telegram import InputFile, Message, Update
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
from ..normalizer import MAX_IMAGE_BYTES
from . import callbacks, commands

if TYPE_CHECKING:
    from ..bot import RemyBot


logger = logging.getLogger("remy.handlers.messages")


# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

# Максимальная длина одного Telegram-сообщения с учётом HTML-тегов.
_TG_MESSAGE_LIMIT: int = 4096

# Видимый текст дисклеймера ИИ (без тегов) — для проверок и документации.
_RECIPE_AI_DISCLAIMER_TEXT: str = "Рецепт обработан ИИ, возможны неточности"

# Часть карточки после основного текста: пустая строка + курсив (HTML).
# Вариант A — деликатно предупреждает о возможных ошибках без подрыва доверия к боту.
_RECIPE_AI_DISCLAIMER_HTML: str = f"\n\n<i>{_RECIPE_AI_DISCLAIMER_TEXT}</i>"

# Суффикс при превышении лимита Telegram; ставится перед дисклеймером.
_TRUNCATED_NOTICE_HTML: str = "\n…\n<i>(сокращено)</i>"

# Регэксп для поиска URL в произвольном сообщении (берём первый http/https).
# Допускаем любой непробельный суффикс — валидацию реальной доступности
# выполняет парсер; лучше ошибиться в сторону «попробовали и узнали».
_URL_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)

# Подсказка, если пользователь прислал что-то непонятное.
_NOT_A_URL_HINT: str = (
    "🤔 Не вижу ссылки. Отправь URL рецепта (начинается с http:// или https://),\n"
    "или нажми 📋 Меню, чтобы увидеть доступные действия."
)

# Лимит подписи к фото (Telegram).
_TG_PHOTO_CAPTION_LIMIT: int = 1024

# Уменьшение превью перед sendPhoto (стабильнее на Railway).
_TG_PHOTO_MAX_SIDE: int = 512
_TG_PHOTO_JPEG_QUALITY: int = 85


# --------------------------------------------------------------------------- #
# Показ рецепта (фото + полный текст)
# --------------------------------------------------------------------------- #


def _jpeg_bytes_for_telegram_photo(image_path: str) -> bytes:
    """Вписать изображение в квадрат до ``_TG_PHOTO_MAX_SIDE`` и вернуть JPEG в памяти."""
    try:
        from PIL import Image
    except ImportError:
        with open(image_path, "rb") as fh:
            return fh.read()
    try:
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            im.thumbnail(
                (_TG_PHOTO_MAX_SIDE, _TG_PHOTO_MAX_SIDE),
                Image.Resampling.LANCZOS,
            )
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=_TG_PHOTO_JPEG_QUALITY, optimize=True)
            return buf.getvalue()
    except (OSError, ValueError) as exc:
        logger.warning(
            "⚠️  Не удалось сжать изображение для Telegram, читаю файл как есть: %s",
            exc,
        )
        with open(image_path, "rb") as fh:
            return fh.read()


# --------------------------------------------------------------------------- #
# Показ рецепта (фото + полный текст) — реализация
# --------------------------------------------------------------------------- #


def _plain_caption_from_html(formatted_html: str, max_len: int = _TG_PHOTO_CAPTION_LIMIT) -> str:
    plain = re.sub(r"<[^>]+>", "", formatted_html)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:max_len]


async def _present_recipe_with_optional_photo(
    message: Message,
    status: Message,
    recipe: Mapping[str, Any],
    bot: "RemyBot",
) -> None:
    formatted = format_recipe(recipe, bot)
    img_path = recipe.get("image_url") or recipe.get("image_path")
    if img_path and os.path.isfile(str(img_path)):
        cap = _plain_caption_from_html(formatted)
        try:
            blob = _jpeg_bytes_for_telegram_photo(str(img_path))
            bio = BytesIO(blob)
            bio.seek(0)
            await message.reply_photo(
                photo=InputFile(bio, filename="recipe.jpg"),
                caption=cap or " ",
                read_timeout=15.0,
                write_timeout=60.0,
            )
            t = recipe.get("title")
            logger.info(
                "Фото отправлено для рецепта %s",
                t if isinstance(t, str) and t.strip() else "без названия",
            )
        except Exception as exc:  # noqa: BLE001 — таймауты httpx/Telegram и I/O
            logger.warning(
                "Не удалось отправить фото рецепта: %s. Отправляю карточку текстом.",
                exc,
            )
        try:
            await message.reply_text(
                formatted,
                reply_markup=save_recipe_keyboard(),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest as exc:
            logger.warning("⚠️  reply_text HTML: %s — без разметки", exc)
            plain = re.sub(r"<[^>]+>", "", formatted)
            await message.reply_text(
                plain[:_TG_MESSAGE_LIMIT],
                reply_markup=save_recipe_keyboard(),
                disable_web_page_preview=True,
            )
        try:
            await status.delete()
        except BadRequest:
            await _safe_edit(status, "✅ Готово")
        return

    try:
        await status.edit_text(
            formatted,
            reply_markup=save_recipe_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as exc:
        logger.warning("⚠️  Ошибка edit_text с HTML: %s — отправляю без разметки", exc)
        plain = re.sub(r"<[^>]+>", "", formatted)
        await status.edit_text(
            plain[:_TG_MESSAGE_LIMIT],
            reply_markup=save_recipe_keyboard(),
            disable_web_page_preview=True,
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
    await message.reply_text(
        _NOT_A_URL_HINT,
        reply_markup=main_menu_keyboard(),
    )


def _guess_image_mime(head: bytes) -> str:
    """Определить MIME по сигнатуре (Telegram обычно JPEG)."""
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Скачать фото, отправить в vision API, показать рецепт или отказ."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not message.photo:
        return

    logger.info("📷 Получено изображение от user %s", user.id)

    photo = message.photo[-1]
    status: Message = await message.reply_text("🔍 Анализирую изображение...")
    bot = _get_bot(context)

    try:
        file = await context.bot.get_file(photo.file_id)
        raw = bytes(await file.download_as_bytearray())
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Не удалось скачать изображение: %s", exc)
        await _safe_edit(
            status,
            f"❌ Не удалось загрузить фото:\n<code>{_html_escape(str(exc))}</code>",
        )
        return

    if len(raw) > MAX_IMAGE_BYTES:
        logger.error("❌ Изображение слишком большое (%s байт)", len(raw))
        await _safe_edit(status, "❌ Изображение слишком большое. Отправьте файл поменьше.")
        return

    b64 = base64.standard_b64encode(raw).decode("ascii")
    mime = _guess_image_mime(raw[:32])

    logger.info("🤖 Анализ изображения через AI...")
    try:
        result = await bot.normalizer.analyze_image(b64, mime_type=mime)
    except ValueError as exc:
        logger.error("❌ Ошибка анализа изображения: %s", exc)
        await _safe_edit(
            status,
            f"❌ Не удалось обработать изображение:\n<code>{_html_escape(str(exc))}</code>",
        )
        return

    if result.get("is_recipe") is False:
        if result.get("error"):
            logger.error("❌ Ошибка API при анализе изображения: %s", result.get("reason"))
            await _safe_edit(
                status,
                "❌ Не удалось проанализировать изображение. Попробуйте позже.",
            )
        else:
            logger.info("ℹ️ Изображение не содержит рецепт")
            await _safe_edit(status, "❌ На этом изображении не удалось найти рецепт.")
        return

    title_ok = bool((result.get("title") or "").strip())
    ingredients_ok = bool(result.get("ingredients"))
    if not (title_ok and ingredients_ok):
        logger.info("ℹ️ Изображение не содержит рецепт (валидация)")
        await _safe_edit(status, "❌ На этом изображении не удалось найти рецепт.")
        return

    result["source_url"] = ""
    if not result.get("image_url") and not result.get("image_path"):
        from ..parser import _generate_and_save_image

        gen_path = await _generate_and_save_image(
            str(result.get("title") or "блюдо")[:400],
        )
        if gen_path:
            result["image_url"] = gen_path

    bot.temp_recipes[user.id] = {"recipe": result, "timestamp": time.time()}
    bot.cleanup_expired_temp_recipes()

    logger.info("✅ Рецепт извлечён из изображения")

    await _present_recipe_with_optional_photo(message, status, result, bot)


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
        parsed = await bot.parser.parse(url)
        raw_text = parsed.text
    except Exception as exc:  # noqa: BLE001 — логируем любую причину
        logger.error("❌ Ошибка обработки URL: %s", exc)
        await _safe_edit(status, f"❌ Не удалось прочитать страницу:\n<code>{_html_escape(str(exc))}</code>")
        return

    if not raw_text or not raw_text.strip():
        logger.warning("⚠️  Пустой текст после парсинга: %s", url)
        await _safe_edit(status, "❌ Со страницы не удалось извлечь текст")
        return

    recipe_data: dict[str, Any] = {
        "raw_text": raw_text,
        "image_url": parsed.image_url,
    }
    src_parser = bot.parser.get_parser(url)
    if src_parser is not None:
        await src_parser.generate_image_if_needed(recipe_data)

    # 2) Нормализация
    await _safe_edit(status, "🤖 Анализирую рецепт...")

    try:
        recipe = await bot.normalizer.normalize(
            recipe_data["raw_text"],
            image_url=recipe_data.get("image_url"),
        )
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

    await _present_recipe_with_optional_photo(message, status, recipe, bot)


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
        В конце добавлен дисклеймер об ИИ; длина гарантированно не превышает
        4096 символов (при обрезке длинного текста дисклеймер сохраняется).
    """
    loc = bot.loc

    meal_type = recipe.get("meal_type") or "other"
    difficulty = recipe.get("difficulty") or "medium"
    cuisine = recipe.get("cuisine") or "other"
    dish_type = recipe.get("dish_type") or "main"
    main_ingredient = recipe.get("main_ingredient") or "other"

    title = _html_escape(str(recipe.get("title") or "Без названия").strip())
    meal_emoji = loc.get_meal_type_emoji(meal_type)
    meal_name = _html_escape(loc.get_meal_type_name(meal_type))
    cuisine_name = _html_escape(loc.get_cuisine_name(cuisine))
    difficulty_display = _html_escape(loc.get_difficulty_display(difficulty))
    dish_line = _html_escape(loc.get_dish_type_display(dish_type))
    main_line = _html_escape(loc.get_main_ingredient_display(main_ingredient))

    lines: List[str] = [
        f"{meal_emoji} <b>{title}</b>",
        "",
        f"{dish_line} | {main_line}",
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

    # Основной текст карточки (до дисклеймера и возможной обрезки).
    body = "\n".join(lines)
    disc = _RECIPE_AI_DISCLAIMER_HTML

    # Дисклеймер всегда в конце; если целиком не влезает — сначала укорачиваем body.
    if len(body) + len(disc) <= _TG_MESSAGE_LIMIT:
        return body + disc

    # Резервируем место под маркер «сокращено» и дисклеймер (как в оригинале — буфер ~40
    # символов, чтобы не резать посередине многобайтового символа / границы тега).
    suffix_total = len(_TRUNCATED_NOTICE_HTML) + len(disc)
    max_main = _TG_MESSAGE_LIMIT - suffix_total
    safe_cut = max(0, max_main - 40)
    truncated = body[:safe_cut].rstrip() + _TRUNCATED_NOTICE_HTML + disc
    return truncated


# Имя из ТЗ / внешних импортов: одна реализация, без дублирования в `utils.py`.
format_recipe_for_telegram = format_recipe


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


# --------------------------------------------------------------------------- #
# Локальная проверка format_recipe (без Telegram)
# --------------------------------------------------------------------------- #


if __name__ == "__main__":
    import os
    import sys
    from importlib import reload
    from pathlib import Path

    _root = Path(__file__).resolve().parent.parent.parent
    sys.path.insert(0, str(_root))

    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:telegram")
    os.environ.setdefault("GITHUB_TOKEN", "test-github")
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_KEY", "test-key")

    import config as _cfg_module  # noqa: E402

    reload(_cfg_module)

    from src.bot import RemyBot  # noqa: E402

    _bot = RemyBot(_cfg_module.config)

    _demo: Mapping[str, Any] = {
        "title": "Тест дисклеймера",
        "meal_type": "lunch",
        "dish_type": "soup",
        "main_ingredient": "beef",
        "difficulty": "medium",
        "cuisine": "russian",
        "ingredients": [{"name": "Вода", "amount": 1, "unit": "л", "notes": ""}],
        "steps": [],
    }

    _out = format_recipe(_demo, _bot)
    assert _RECIPE_AI_DISCLAIMER_TEXT in _out

    _long = dict(_demo)
    _long["steps"] = [{"step_number": 1, "description": "Д" * 12000}]
    _out_long = format_recipe(_long, _bot)
    assert _RECIPE_AI_DISCLAIMER_TEXT in _out_long
    assert len(_out_long) <= _TG_MESSAGE_LIMIT

    import asyncio
    import tempfile
    from unittest.mock import AsyncMock, MagicMock

    _tmp_img = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    _tmp_img.write(b"\xff\xd8\xff\xd9")
    _tmp_img.close()
    try:
        _mock_msg = MagicMock()
        _mock_msg.reply_photo = AsyncMock()
        _mock_msg.reply_text = AsyncMock()
        _mock_status = MagicMock()
        _mock_status.delete = AsyncMock()
        _demo_img = dict(_demo)
        _demo_img["image_url"] = _tmp_img.name

        async def _run_present() -> None:
            await _present_recipe_with_optional_photo(_mock_msg, _mock_status, _demo_img, _bot)

        asyncio.run(_run_present())
        _mock_msg.reply_photo.assert_called_once()
        _mock_msg.reply_text.assert_called_once()
    finally:
        os.unlink(_tmp_img.name)

    print("✅ format_recipe + _present_recipe_with_optional_photo (мок)")
