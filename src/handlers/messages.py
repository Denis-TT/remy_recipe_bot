"""
Обработчик текстовых сообщений Remy Bot.

Маршрутизирует входящие сообщения:

* Нажатия на кнопки Reply-клавиатуры («📋 Меню», «📚 Сохраненные рецепты»,
  «ℹ️ Помощь») — открывают соответствующий экран;
* Фото — анализ через GitHub Models (vision), нормализация, карточка с кнопками;
* URL (http/https) — цепочка «парсинг → нормализация → одно сообщение с кнопками»
  (при наличии картинки — фото с HTML-подписью до 1024 символов и те же кнопки);
* Текст рецепта (пересланный или вставленный) — нормализация через AI без парсера;
* Любой другой текст — короткая подсказка отправить ссылку или рецепт.

Дополнительно: :func:`format_recipe` и псевдоним :func:`format_recipe_for_telegram`
(одна реализация) — HTML-карточка рецепта для Telegram.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import time
from html import escape as _html_escape
from io import BytesIO
from typing import TYPE_CHECKING, Any, List, Mapping, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message, Update
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
from ..localization import Localization
from ..ytdlp_mixin import VideoPolicyError
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
    "🤔 Не вижу ссылки и не похоже на рецепт. Отправь:\n"
    "• ссылку на рецепт (http:// или https://)\n"
    "• или текст рецепта (ингредиенты и шаги)\n"
    "• или нажми 📋 Меню, чтобы увидеть доступные действия."
)

# Минимальная длина текста, чтобы пробовать распознать рецепт.
_RECIPE_TEXT_MIN_CHARS: int = 35

# Признаки рецепта в свободном тексте (ингредиенты, шаги, единицы измерения).
_RECIPE_TEXT_SIGNAL_RE = re.compile(
    r"(?:"
    r"ингредиент|состав|понадобится|понадобятся|нужно:|"
    r"готовк|приготов|нарез|смешай|запек|варить|тушить|"
    r"рецепт|"
    r"\d+\s*(?:г|кг|мл|л|шт|ст\.?\s*л\.?|ч\.?\s*л\.?)\b|"
    r"(?:^|\n)\s*\d+[\.\)]\s"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# Длинный текст без URL — часто пересланный рецепт из мессенджера.
_RECIPE_TEXT_LONG_CHARS: int = 120

# Лимит подписи к фото (Telegram).
_TG_PHOTO_CAPTION_LIMIT: int = 1024

# Целевая длина основного текста подписи до «…», чтобы с запасом влезли закрывающие теги.
_TG_PHOTO_CAPTION_BODY_MAX: int = 1000

# Уменьшение превью перед sendPhoto (стабильнее на Railway).
_TG_PHOTO_MAX_SIDE: int = 512
_TG_PHOTO_JPEG_QUALITY: int = 85

# Публичная ссылка на бота в сообщениях «Поделиться рецептом».
_REMY_BOT_URL: str = "https://t.me/remy_recipe_bot"
_REMY_BOT_USERNAME: str = "@remy_recipe_bot"


# --------------------------------------------------------------------------- #
# Показ рецепта (с фото — одно сообщение; без фото — правка статуса)
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
# Показ рецепта (фото + подпись) — реализация
# --------------------------------------------------------------------------- #


_HTML_TAG_TOKEN_RE = re.compile(r"<(/?)\s*([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>", re.IGNORECASE)

# Теги без тела в Telegram HTML (и обычные void) — не кладём в стек.
_HTML_VOID_TAGS = frozenset({"br", "tg-emoji"})


def _close_open_html_tags(fragment: str) -> str:
    """Дозакрыть незакрытые открывающие теги в конце HTML-фрагмента."""
    stack: List[str] = []
    for m in _HTML_TAG_TOKEN_RE.finditer(fragment):
        is_close = m.group(1) == "/"
        name = m.group(2).lower()
        if not is_close and name in _HTML_VOID_TAGS:
            continue
        if is_close:
            while stack and stack[-1] != name:
                stack.pop()
            if stack and stack[-1] == name:
                stack.pop()
        else:
            stack.append(name)
    if not stack:
        return fragment
    return fragment + "".join(f"</{t}>" for t in reversed(stack))


def _local_recipe_image_path(recipe: Mapping[str, Any]) -> Optional[str]:
    """Локальный путь к файлу для ``reply_photo`` (не HTTP URL из Storage)."""
    for key in ("image_path", "image_url"):
        p = str(recipe.get(key) or "").strip()
        if not p or p.startswith(("http://", "https://")):
            continue
        if os.path.isfile(p):
            return p
    return None


def _html_caption_for_photo(formatted_html: str) -> str:
    """Подпись к фото: HTML, не длиннее лимита Telegram; при обрезке — «…» и целые теги."""
    if len(formatted_html) <= _TG_PHOTO_CAPTION_LIMIT:
        return formatted_html

    ell = "…"
    reserve = len(ell) + 48
    budget = max(100, min(_TG_PHOTO_CAPTION_BODY_MAX, _TG_PHOTO_CAPTION_LIMIT - reserve))
    cut = formatted_html[: budget]
    sp = cut.rfind(" ")
    if sp > budget // 2:
        cut = cut[:sp]
    cut = re.sub(r"<[^>]*$", "", cut)
    cut = _close_open_html_tags(cut).strip()
    out = cut + ell
    if len(out) > _TG_PHOTO_CAPTION_LIMIT:
        plain = re.sub(r"<[^>]+>", "", formatted_html)
        plain = plain[: _TG_PHOTO_CAPTION_LIMIT - len(ell)].rsplit(" ", 1)[0].strip()
        return _html_escape(plain) + ell
    return out


def _compact_photo_caption(recipe: Mapping[str, Any], bot: "RemyBot") -> str:
    """Короткая подпись к фото: только заголовок и одна строка метаданных."""
    loc = bot.loc
    title = _html_escape(str(recipe.get("title") or "Без названия").strip())
    meal_type = recipe.get("meal_type") or "other"
    meal_emoji = loc.get_meal_type_emoji(meal_type)
    cuisine = _html_escape(loc.get_cuisine_name(recipe.get("cuisine") or "other"))
    total = _int(recipe.get("total_time"))
    parts = [f"{meal_emoji} <b>{title}</b>", "", cuisine]
    if total:
        parts.append(f"⏱ {total} мин")
    parts.append("")
    parts.append("<i>Полный рецепт — в следующем сообщении</i>")
    return "\n".join(parts)


async def _send_recipe_html_messages(
    message: Message,
    formatted_html: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Отправить длинный HTML-рецепт одним или несколькими сообщениями."""
    text = formatted_html
    if len(text) <= _TG_MESSAGE_LIMIT:
        await message.reply_text(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    chunks: List[str] = []
    rest = text
    while rest:
        if len(rest) <= _TG_MESSAGE_LIMIT:
            chunks.append(rest)
            break
        cut = rest[: _TG_MESSAGE_LIMIT - 80]
        sp = cut.rfind("\n\n")
        if sp < _TG_MESSAGE_LIMIT // 3:
            sp = cut.rfind("\n")
        if sp > _TG_MESSAGE_LIMIT // 4:
            cut = rest[:sp].rstrip()
        else:
            cut = rest[: _TG_MESSAGE_LIMIT - 80].rstrip()
        cut = _close_open_html_tags(re.sub(r"<[^>]*$", "", cut).rstrip())
        chunks.append(cut)
        rest = rest[len(cut) :].lstrip()

    for idx, chunk in enumerate(chunks):
        kb = reply_markup if idx == len(chunks) - 1 else None
        try:
            await message.reply_text(
                chunk,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except BadRequest:
            plain = re.sub(r"<[^>]+>", "", chunk)
            await message.reply_text(
                plain[:_TG_MESSAGE_LIMIT],
                reply_markup=kb,
                disable_web_page_preview=True,
            )


async def _present_recipe_with_optional_photo(
    message: Message,
    status: Message,
    recipe: Mapping[str, Any],
    bot: "RemyBot",
) -> None:
    formatted = format_recipe(recipe, bot)
    kb = save_recipe_keyboard()
    img_path = _local_recipe_image_path(recipe)
    if img_path:
        caption_html = _compact_photo_caption(recipe, bot)
        try:
            blob = _jpeg_bytes_for_telegram_photo(str(img_path))
            bio = BytesIO(blob)
            bio.seek(0)
            await message.reply_photo(
                photo=InputFile(bio, filename="recipe.jpg"),
                caption=caption_html or " ",
                parse_mode=ParseMode.HTML,
                read_timeout=15.0,
                write_timeout=60.0,
            )
            t = recipe.get("title")
            logger.info(
                "Фото отправлено для рецепта %s",
                t if isinstance(t, str) and t.strip() else "без названия",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Не удалось отправить фото рецепта: %s. Отправляю только текст.",
                exc,
            )
        await _send_recipe_html_messages(message, formatted, kb)
        try:
            await status.delete()
        except BadRequest:
            await _safe_edit(status, "✅ Готово")
        return

    try:
        if len(formatted) <= _TG_MESSAGE_LIMIT:
            await status.edit_text(
                formatted,
                reply_markup=kb,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await status.edit_text("✅ Готово", reply_markup=None)
            await _send_recipe_html_messages(message, formatted, kb)
    except BadRequest as exc:
        logger.warning("⚠️  Ошибка edit_text с HTML: %s — отправляю без разметки", exc)
        plain = re.sub(r"<[^>]+>", "", formatted)
        await status.edit_text(
            plain[:_TG_MESSAGE_LIMIT],
            reply_markup=kb,
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

    if await try_handle_pending_share(message, context, user.id):
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

    # Свободный текст → рецепт без ссылки.
    if _looks_like_recipe_text(raw_text):
        await _handle_text_recipe(message, context, user.id, raw_text)
        return

    # Всё остальное — подсказка.
    await message.reply_text(
        _NOT_A_URL_HINT,
        reply_markup=main_menu_keyboard(),
    )


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработать команды из Telegram Mini App через WebAppData."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or message.web_app_data is None:
        return

    raw = message.web_app_data.data or ""
    logger.info("📲 WEB_APP_DATA от user %s: %s", user.id, raw[:200])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("Некорректный WebAppData JSON: %s", exc)
        await message.reply_text("❌ Не удалось обработать запрос из Mini App.")
        return

    if not isinstance(data, Mapping):
        logger.warning("Некорректный WebAppData payload: %r", data)
        return

    action = str(data.get("action") or "").strip()
    if action != "share_recipe":
        logger.info("Неизвестное действие WebAppData: %s", action)
        return

    recipe_id = str(data.get("recipe_id") or "").strip()
    if not recipe_id:
        await message.reply_text("❌ Не удалось определить рецепт для шаринга.")
        return

    bot = _get_bot(context)
    try:
        recipe = await bot.storage.get_recipe(recipe_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Ошибка получения рецепта для WebApp share %s: %s", recipe_id, exc)
        await message.reply_text("❌ Не удалось загрузить рецепт для шаринга.")
        return

    if recipe is None or not _recipe_belongs_to_user(recipe, user.id):
        await message.reply_text("❌ Рецепт не найден.")
        return

    try:
        await _send_shared_recipe(message.chat_id, recipe, context.bot)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ WebApp share %s: ошибка отправки: %s", recipe_id, exc, exc_info=True)
        await message.reply_text("❌ Не удалось отправить рецепт. Попробуй ещё раз.")


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
        from ..parser import _generate_and_save_image, attach_recipe_image

        gen_path = await _generate_and_save_image(
            str(result.get("title") or "блюдо")[:400],
            hf_api_key=bot.config.hf_api_key,
        )
        if gen_path:
            await attach_recipe_image(result, gen_path, bot.storage)

    bot.temp_recipes[user.id] = {"recipe": result, "timestamp": time.time()}
    bot.cleanup_expired_temp_recipes()

    logger.info("✅ Рецепт извлечён из изображения")

    await _present_recipe_with_optional_photo(message, status, result, bot)


# --------------------------------------------------------------------------- #
# Прогресс обработки URL (чеклист этапов в одном статусном сообщении)
# --------------------------------------------------------------------------- #

# Этапы коротких видео (Instagram, YouTube, TikTok, VK).
_VIDEO_PROGRESS_STEPS: tuple[tuple[str, str], ...] = (
    ("metadata", "Читаю описание видео"),
    ("transcribe", "Распознаю речь"),
    ("normalize", "Анализирую рецепт"),
    ("present", "Формирую карточку"),
)

# Этапы для текста рецепта (без парсера URL).
_TEXT_PROGRESS_STEPS: tuple[tuple[str, str], ...] = (
    ("parse", "Читаю текст"),
    ("normalize", "Анализирую рецепт"),
    ("present", "Формирую карточку"),
)

# Этапы для обычных сайтов.
_DEFAULT_PROGRESS_STEPS: tuple[tuple[str, str], ...] = (
    ("parse", "Читаю страницу"),
    ("normalize", "Анализирую рецепт"),
    ("present", "Формирую карточку"),
)

_VIDEO_PARSER_STAGE_TO_STEP: dict[str, str] = {
    "fetching_metadata": "metadata",
    "downloading_audio": "transcribe",
    "transcribing": "transcribe",
    "apify_fallback": "transcribe",
}

_VIDEO_SOURCE_TYPES = frozenset({"instagram", "youtube", "tiktok", "vk"})


class RecipeProgress:
    """Чеклист этапов: одно сообщение в Telegram, обновляется по ходу пайплайна."""

    def __init__(self, status: Message, *, video: bool = False, source_type: str = "") -> None:
        self._status = status
        if source_type == "text":
            self._steps = _TEXT_PROGRESS_STEPS
        elif video:
            self._steps = _VIDEO_PROGRESS_STEPS
        else:
            self._steps = _DEFAULT_PROGRESS_STEPS
        self._completed: set[str] = set()
        self._current: Optional[str] = None
        self._detail = ""
        if source_type == "instagram":
            self._title = "📸 <b>Instagram Reel</b>"
        elif source_type == "youtube":
            self._title = "▶️ <b>YouTube</b>"
        elif source_type == "tiktok":
            self._title = "🎵 <b>TikTok</b>"
        elif source_type == "vk":
            self._title = "📺 <b>VK Видео</b>"
        elif source_type == "text":
            self._title = "📝 <b>Текст рецепта</b>"
        elif video:
            self._title = "🎬 <b>Видео</b>"
        else:
            self._title = "🔍 <b>Обработка ссылки</b>"

    async def start(self) -> None:
        """Показать чеклист, первый этап — активный."""
        if self._steps:
            self._current = self._steps[0][0]
        await self._render()

    async def set_stage(self, stage_id: str, detail: str = "") -> None:
        """Переключить активный этап; предыдущие отметить выполненными."""
        self._detail = detail
        found = False
        for sid, _label in self._steps:
            if sid == stage_id:
                found = True
                self._current = stage_id
                break
            if not found:
                self._completed.add(sid)
        if not found:
            self._current = stage_id
        await self._render()

    def _render_text(self) -> str:
        total = len(self._steps)
        lines = [self._title, ""]
        for idx, (sid, label) in enumerate(self._steps, 1):
            prefix = f"{idx}/{total}"
            if sid in self._completed:
                lines.append(f"✅ {prefix} {label}")
            elif sid == self._current:
                suffix = f" — {self._detail}" if self._detail else "…"
                lines.append(f"🔄 {prefix} {label}{suffix}")
            else:
                lines.append(f"⏳ {prefix} {label}")
        return "\n".join(lines)

    async def _render(self) -> None:
        await _safe_edit(self._status, self._render_text())


# --------------------------------------------------------------------------- #
# Текстовый рецепт (без URL)
# --------------------------------------------------------------------------- #


def _looks_like_recipe_text(text: str) -> bool:
    """Эвристика: похоже ли сообщение на рецепт, а не на болтовню."""
    t = (text or "").strip()
    if len(t) < _RECIPE_TEXT_MIN_CHARS:
        return False
    if _RECIPE_TEXT_SIGNAL_RE.search(t):
        return True
    if len(t) >= _RECIPE_TEXT_LONG_CHARS:
        return True
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    bullet_lines = sum(1 for ln in lines if re.search(r"^[-•—*]\s", ln))
    return len(lines) >= 4 and bullet_lines >= 2


def _text_processing_key(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"txt:{digest}"


async def _handle_text_recipe(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    raw_text: str,
) -> None:
    """Нормализовать свободный текст рецепта и показать карточку."""
    bot = _get_bot(context)
    job_key = _text_processing_key(raw_text)

    if bot.processing_urls.get(user_id) == job_key:
        await message.reply_text(
            "⏳ Этот текст уже обрабатывается. Подожди — скоро пришлю результат.",
        )
        return

    if user_id in bot.processing_urls:
        await message.reply_text(
            "⏳ Уже обрабатываю предыдущий запрос. Подожди — скоро пришлю результат.",
        )
        return

    bot.processing_urls[user_id] = job_key

    status: Message = await message.reply_text("⏳ Запускаю обработку…")
    progress = RecipeProgress(status, source_type="text")
    await progress.start()

    try:
        await progress.set_stage("parse")
        text = raw_text.strip()
        if not text:
            await _safe_edit(status, "❌ Пустое сообщение")
            return

        await progress.set_stage("normalize")
        try:
            recipe = await bot.normalizer.normalize(text)
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ Ошибка нормализации текста: %s", exc)
            await _safe_edit(
                status,
                f"❌ Не удалось обработать рецепт:\n<code>{_html_escape(str(exc))}</code>",
            )
            return

        title_ok = bool((recipe.get("title") or "").strip())
        ingredients_ok = bool(recipe.get("ingredients"))
        if not (title_ok and ingredients_ok):
            logger.warning(
                "⚠️  Текст не прошёл валидацию рецепта (title=%s, ingredients=%s)",
                title_ok,
                ingredients_ok,
            )
            await _safe_edit(
                status,
                "❌ Не удалось распознать рецепт в этом тексте.\n"
                "Проверь, что есть название, ингредиенты и шаги приготовления.",
            )
            return

        recipe["source_url"] = ""
        if not recipe.get("image_url") and not recipe.get("image_path"):
            from ..parser import _generate_and_save_image, attach_recipe_image

            gen_path = await _generate_and_save_image(
                str(recipe.get("title") or "блюдо")[:400],
                hf_api_key=bot.config.hf_api_key,
            )
            if gen_path:
                await attach_recipe_image(recipe, gen_path, bot.storage)

        bot.temp_recipes[user_id] = {"recipe": recipe, "timestamp": time.time()}
        bot.cleanup_expired_temp_recipes()

        logger.info(
            "✅ Рецепт из текста: «%s», meal_type=%s",
            recipe.get("title"),
            recipe.get("meal_type"),
        )

        await progress.set_stage("present")
        await _present_recipe_with_optional_photo(message, status, recipe, bot)
    finally:
        if bot.processing_urls.get(user_id) == job_key:
            bot.processing_urls.pop(user_id, None)


# --------------------------------------------------------------------------- #
# URL-пайплайн
# --------------------------------------------------------------------------- #

def _url_processing_key(url: str, source_type: str) -> str:
    """Ключ для дедупликации: shortcode Instagram или нормализованный URL."""
    if source_type == "instagram":
        from ..parser import InstagramParser

        shortcode = InstagramParser._extract_shortcode(url)
        if shortcode:
            return f"ig:{shortcode}"
    if source_type in ("youtube", "tiktok", "vk"):
        from ..parser import TikTokParser, VkVideoParser, YouTubeParser

        if source_type == "youtube":
            vid = YouTubeParser._extract_youtube_video_id(url)
            if vid:
                return f"yt:{vid}"
        elif source_type == "vk":
            vid = VkVideoParser._extract_video_id(url)
            if vid:
                return f"vk:{vid}"
        else:
            normalized = url.strip().lower().rstrip("/")
            return f"tt:{normalized}"
    return url.strip().lower().rstrip("/")


async def _handle_url(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    url: str,
    *,
    skip_rate_limit: bool = False,
) -> None:
    """Распарсить URL, нормализовать рецепт и отправить результат."""
    bot = _get_bot(context)

    logger.info("🔍 Начинаю обработку URL: %s", url)

    if not skip_rate_limit:
        allowed, wait_sec = bot.url_rate_limiter.check(user_id)
        if not allowed:
            limit_sec = bot.config.url_rate_limit_seconds
            if limit_sec % 60 == 0:
                limit_label = f"{limit_sec // 60} мин"
            else:
                limit_label = f"{limit_sec} сек"
            await message.reply_text(
                f"⏳ Слишком часто. Подожди {wait_sec} сек. перед следующей ссылкой "
                f"(лимит: 1 ссылка раз в {limit_label}).",
            )
            return

    if user_id in bot.processing_urls:
        await message.reply_text(
            "⏳ Уже обрабатываю предыдущий запрос. Подожди — скоро пришлю результат.",
        )
        return

    src_parser = bot.parser.get_parser(url)
    source_type = getattr(src_parser, "source_type", "") if src_parser else ""
    job_key = _url_processing_key(url, source_type)

    bot.processing_urls[user_id] = job_key
    if not skip_rate_limit:
        bot.url_rate_limiter.record(user_id)

    status: Message = await message.reply_text("⏳ Запускаю обработку…")
    is_video = source_type in _VIDEO_SOURCE_TYPES
    progress = RecipeProgress(status, video=is_video, source_type=source_type)
    await progress.start()

    async def _on_parser_progress(stage: str, detail: str = "") -> None:
        step_id = _VIDEO_PARSER_STAGE_TO_STEP.get(stage, stage)
        if stage == "apify_fallback":
            await progress.set_stage(step_id, detail or "запасной путь")
        elif detail:
            await progress.set_stage(step_id, detail)
        else:
            await progress.set_stage(step_id)

    try:
        # 1) Парсинг
        try:
            if is_video:
                await progress.set_stage("metadata")
            else:
                await progress.set_stage("parse")
            parsed = await bot.parser.parse(
                url,
                on_progress=_on_parser_progress if is_video else None,
            )
            raw_text = parsed.text
        except VideoPolicyError as exc:
            logger.info("ℹ️ Ограничение по видео: %s", exc)
            await _safe_edit(status, f"❌ {_html_escape(str(exc))}")
            return
        except Exception as exc:  # noqa: BLE001 — логируем любую причину
            logger.error("❌ Ошибка обработки URL: %s", exc)
            await _safe_edit(status, f"❌ Не удалось прочитать страницу:\n<code>{_html_escape(str(exc))}</code>")
            return

        if not raw_text or not raw_text.strip():
            logger.warning("⚠️  Пустой текст после парсинга: %s", url)
            await _safe_edit(status, "❌ Со страницы не удалось извлечь текст")
            return

        from ..parser import attach_recipe_image

        recipe_data: dict[str, Any] = {"raw_text": raw_text}
        if parsed.image_url and os.path.isfile(str(parsed.image_url)):
            await attach_recipe_image(recipe_data, str(parsed.image_url), bot.storage)
        elif parsed.image_url:
            recipe_data["image_url"] = parsed.image_url

        if src_parser is not None:
            await src_parser.generate_image_if_needed(
                recipe_data,
                hf_api_key=bot.config.hf_api_key,
                storage=bot.storage,
            )

        # 2) Нормализация
        await progress.set_stage("normalize")

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
        if recipe_data.get("image_path"):
            recipe["image_path"] = recipe_data["image_path"]
        img_pub = recipe_data.get("image_url")
        if img_pub and str(img_pub).startswith(("http://", "https://")):
            recipe["image_url"] = str(img_pub).strip()

        bot.temp_recipes[user_id] = {"recipe": recipe, "timestamp": time.time()}
        bot.cleanup_expired_temp_recipes()

        logger.info(
            "✅ Рецепт обработан: «%s», meal_type=%s",
            recipe.get("title"),
            recipe.get("meal_type"),
        )

        await progress.set_stage("present")
        await _present_recipe_with_optional_photo(message, status, recipe, bot)
    finally:
        if bot.processing_urls.get(user_id) == job_key:
            bot.processing_urls.pop(user_id, None)


# --------------------------------------------------------------------------- #
# Форматирование рецепта
# --------------------------------------------------------------------------- #

def _nutrition_num(value: Any, *, estimated: bool) -> str:
    n = _int(value)
    if not n:
        return "0"
    return f"~{n}" if estimated else str(n)


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
    nutrition_note = str(recipe.get("nutrition_note") or "").strip()
    nutrition_estimated = bool(recipe.get("nutrition_estimated"))
    nutrition = recipe.get("nutrition_per_serving") or {}
    if nutrition_note and not (
        isinstance(nutrition, Mapping)
        and any(_int(nutrition.get(k)) for k in ("calories", "protein", "fat", "carbs"))
    ):
        lines.extend(["", f"📊 КБЖУ: {_html_escape(nutrition_note)}"])
    elif isinstance(nutrition, Mapping):
        cal = _int(nutrition.get("calories"))
        protein = _int(nutrition.get("protein"))
        fat = _int(nutrition.get("fat"))
        carbs = _int(nutrition.get("carbs"))
        if cal or protein or fat or carbs:
            est_label = " (примерно)" if nutrition_estimated else ""
            lines.extend([
                "",
                f"📊 КБЖУ на порцию{est_label}:",
                (
                    f"🔥 {_nutrition_num(cal, estimated=nutrition_estimated)} ккал | "
                    f"💪 {_nutrition_num(protein, estimated=nutrition_estimated)} г | "
                    f"🧈 {_nutrition_num(fat, estimated=nutrition_estimated)} г | "
                    f"🍚 {_nutrition_num(carbs, estimated=nutrition_estimated)} г"
                ),
            ])
            if nutrition_note:
                lines.append(_html_escape(nutrition_note))

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
# Шаринг / очередь Mini App
# --------------------------------------------------------------------------- #


async def try_handle_pending_share(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    """Если Mini App поставил рецепт в очередь — отправить карточку в чат."""
    bot = _get_bot(context)
    try:
        recipe_id = await bot.storage.consume_pending_share(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ pending_shares: %s", exc)
        return False

    if not recipe_id:
        return False

    logger.info("📤 Обработка pending_shares для user %s, recipe %s", user_id, recipe_id)
    try:
        recipe = await bot.storage.get_recipe(recipe_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ pending_shares: загрузка %s: %s", recipe_id, exc)
        await message.reply_text("❌ Не удалось загрузить рецепт для отправки.")
        return True

    if recipe is None or not _recipe_belongs_to_user(recipe, user_id):
        await message.reply_text("❌ Рецепт не найден.")
        return True

    try:
        await _send_shared_recipe(message.chat_id, recipe, context.bot)
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ pending_shares: отправка %s: %s", recipe_id, exc)
        await message.reply_text("❌ Не удалось отправить рецепт. Попробуй ещё раз.")
    return True


# --------------------------------------------------------------------------- #
# Шаринг рецепта (карточка для пересылки)
# --------------------------------------------------------------------------- #

def _recipe_belongs_to_user(recipe: Mapping[str, Any], user_id: int) -> bool:
    owner = recipe.get("user_id")
    if owner is None:
        return True
    try:
        return int(owner) == int(user_id)
    except (TypeError, ValueError):
        return False


def _shared_recipe_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Открыть Remy", url=_REMY_BOT_URL)],
    ])


def _public_image_url(recipe: Mapping[str, Any]) -> str:
    img = str(recipe.get("image_url") or "").strip()
    if img.startswith(("http://", "https://")):
        return img
    return ""


def _shared_meta_line(recipe: Mapping[str, Any], loc: Localization) -> str:
    cuisine = loc.get_cuisine_name(recipe.get("cuisine") or "other")
    dish = loc.get_dish_type_display(recipe.get("dish_type") or "main")
    total = _int(recipe.get("total_time"))
    nutrition = recipe.get("nutrition_per_serving") or {}
    nutrition_note = str(recipe.get("nutrition_note") or "").strip()
    calories = _int(nutrition.get("calories")) if isinstance(nutrition, Mapping) else 0

    parts: List[str] = []
    if cuisine:
        parts.append(f"🍽 {cuisine}")
    if dish:
        parts.append(f"📋 {dish}")
    if total:
        parts.append(f"⏱ {total} мин")
    if nutrition_note:
        parts.append("📊 КБЖУ: см. карточку")
    elif calories:
        parts.append(f"🔥 {calories} ккал/порция")
    return " | ".join(parts)


def _format_shared_recipe(recipe: Mapping[str, Any]) -> str:
    """Собрать красивый HTML-текст для пересылки рецепта другим пользователям."""
    loc = Localization("ru")
    title = _html_escape(str(recipe.get("title") or "Без названия").strip())
    lines: List[str] = [f"<b>🍲 {title}</b>"]

    meta = _shared_meta_line(recipe, loc)
    if meta:
        lines.extend(["", _html_escape(meta)])

    ingredients = recipe.get("ingredients") or []
    if ingredients:
        lines.extend(["", "🛒 <b>Ингредиенты:</b>"])
        for ing in list(ingredients)[:20]:
            lines.append("• " + _html_escape(_format_ingredient(ing)))

    steps = recipe.get("steps") or []
    if steps:
        lines.extend(["", "📝 <b>Приготовление:</b>"])
        for idx, step in enumerate(list(steps)[:12], 1):
            desc = str(step.get("description") or "").strip() if isinstance(step, Mapping) else str(step).strip()
            if not desc:
                continue
            num = _int(step.get("step_number")) if isinstance(step, Mapping) else idx
            lines.append(f"{num or idx}. {_html_escape(desc)}")

    lines.extend([
        "",
        "👨‍🍳 <b>Рецепт приготовлен ботом Remy!</b>",
        f"Попробуй и ты: {_html_escape(_REMY_BOT_USERNAME)}",
    ])
    return "\n".join(lines)


def _truncate_html_message(text: str, limit: int) -> str:
    """Укоротить HTML-сообщение, сохранив закрывающие теги."""
    if len(text) <= limit:
        return text
    ell = "\n…"
    budget = max(0, limit - len(ell) - 32)
    cut = text[:budget]
    sp = cut.rfind("\n")
    if sp > budget // 2:
        cut = cut[:sp]
    cut = re.sub(r"<[^>]*$", "", cut).rstrip()
    return _close_open_html_tags(cut) + ell


async def _send_shared_recipe(chat_id: int, recipe: Mapping[str, Any], bot: Any) -> None:
    """Отправить рецепт для шаринга с промо-кнопкой Remy."""
    logger.info(
        "📤 _send_shared_recipe: chat %s, recipe %s",
        chat_id,
        recipe.get("id") or "—",
    )
    text = _format_shared_recipe(recipe)
    markup = _shared_recipe_markup()
    title = str(recipe.get("title") or "Без названия").strip()
    image_url = _public_image_url(recipe)

    if image_url:
        short_caption = (
            f"<b>🍲 {_html_escape(title)}</b>\n\n"
            "<i>Полный рецепт — в следующем сообщении</i>"
        )
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=image_url,
                caption=short_caption,
                parse_mode=ParseMode.HTML,
            )
            await bot.send_message(
                chat_id=chat_id,
                text=_truncate_html_message(text, _TG_MESSAGE_LIMIT),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            logger.info("Рецепт %s отправлен для шаринга (фото + текст)", title)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось отправить shared recipe с фото: %s; отправляю текстом", exc)

    await bot.send_message(
        chat_id=chat_id,
        text=_truncate_html_message(text, _TG_MESSAGE_LIMIT),
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
        disable_web_page_preview=True,
    )
    logger.info("Рецепт %s отправлен для шаринга", title)


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

    _mock_status_msg = MagicMock()
    _prog = RecipeProgress(_mock_status_msg, video=True, source_type="instagram")
    _prog._current = "transcribe"
    _prog._completed.add("metadata")
    _prog._detail = "42 с"
    _progress_text = _prog._render_text()
    assert "📸" in _progress_text and "1/4" in _progress_text
    assert "✅ 1/4 Читаю описание видео" in _progress_text
    assert "🔄 2/4 Распознаю речь — 42 с" in _progress_text
    assert "⏳ 3/4 Анализирую рецепт" in _progress_text
    print("✅ RecipeProgress чеклист (Instagram)")

    _text_prog = RecipeProgress(_mock_status_msg, source_type="text")
    _text_prog._current = "normalize"
    _text_prog._completed.add("parse")
    _text_render = _text_prog._render_text()
    assert "📝" in _text_render and "1/3 Читаю текст" in _text_render
    assert "🔄 2/3 Анализирую рецепт" in _text_render
    print("✅ RecipeProgress чеклист (текст)")

    _sample_recipe = (
        "Блины классические\n\n"
        "Ингредиенты:\n"
        "мука 500 г\n"
        "молоко 800 мл\n"
        "яйца 3 шт\n"
        "соль 1 ч.л.\n\n"
        "1. Смешать все ингредиенты.\n"
        "2. Жарить на сковороде с двух сторон."
    )
    assert _looks_like_recipe_text(_sample_recipe)
    assert not _looks_like_recipe_text("привет")
    assert not _looks_like_recipe_text("ок")
    assert _looks_like_recipe_text("а" * 130)
    print("✅ _looks_like_recipe_text")

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
        _photo_kwargs = _mock_msg.reply_photo.call_args.kwargs
        assert _photo_kwargs.get("parse_mode") == ParseMode.HTML
        assert _photo_kwargs.get("reply_markup") is None
        assert len(_photo_kwargs.get("caption") or "") <= _TG_PHOTO_CAPTION_LIMIT
        assert "следующем сообщении" in (_photo_kwargs.get("caption") or "")
        _mock_msg.reply_text.assert_called_once()
        _text_call = _mock_msg.reply_text.call_args
        _text_body = (_text_call.args[0] if _text_call.args else "") or _text_call.kwargs.get("text") or ""
        _text_kwargs = _text_call.kwargs
        assert _text_kwargs.get("reply_markup") is not None
        assert _RECIPE_AI_DISCLAIMER_TEXT in _text_body
    finally:
        os.unlink(_tmp_img.name)

    _huge = dict(_demo)
    _huge["steps"] = [{"step_number": 1, "description": "Шаг " + "длинный " * 800}]
    _cap_src = format_recipe(_huge, _bot)
    _cap = _html_caption_for_photo(_cap_src)
    assert len(_cap) <= _TG_PHOTO_CAPTION_LIMIT
    assert "…" in _cap or len(_cap_src) <= _TG_PHOTO_CAPTION_LIMIT

    _shared = dict(_demo)
    _shared.update({
        "title": "Блины",
        "cuisine": "russian",
        "dish_type": "baking",
        "total_time": 30,
        "nutrition_per_serving": {"calories": 350},
        "ingredients": [
            {"name": "Мука", "amount": 500, "unit": "г", "notes": ""},
            {"name": "Яйца", "amount": 2, "unit": "шт", "notes": ""},
        ],
        "steps": [
            {"step_number": 1, "description": "Разогреть сковороду."},
            {"step_number": 2, "description": "Смешать все ингредиенты."},
        ],
    })
    _shared_text = _format_shared_recipe(_shared)
    assert "🍲 Блины" in _shared_text
    assert "500 г Мука" in _shared_text
    assert "2 шт Яйца" in _shared_text
    assert "Рецепт приготовлен ботом Remy" in _shared_text
    assert _REMY_BOT_USERNAME in _shared_text

    _mock_bot = MagicMock()
    _mock_bot.send_message = AsyncMock()
    _mock_bot.send_photo = AsyncMock()

    async def _run_share_text() -> None:
        await _send_shared_recipe(123, _shared, _mock_bot)

    asyncio.run(_run_share_text())
    _mock_bot.send_message.assert_called_once()
    _share_kwargs = _mock_bot.send_message.call_args.kwargs
    assert _share_kwargs.get("parse_mode") == ParseMode.HTML
    assert _share_kwargs.get("reply_markup") is not None
    assert len(_share_kwargs.get("text") or "") <= _TG_MESSAGE_LIMIT

    print("✅ format_recipe + _present_recipe_with_optional_photo (мок)")
