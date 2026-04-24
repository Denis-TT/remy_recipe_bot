"""
Клавиатуры Remy Bot.

Содержит функции-фабрики, возвращающие готовые объекты клавиатур
Telegram. Весь код, формирующий визуальные элементы интерфейса
(Reply/Inline-клавиатуры, кнопки меню, категорий, рецепта), собран
здесь — чтобы хендлеры остались максимально тонкими и сосредоточились
на бизнес-логике.

Важные инварианты:

* `callback_data` **всегда латиница** (`cat_lunch`, `view_<uuid>`, ...).
  Локализованный текст на кнопках берётся из `Localization`, но
  передаваемые данные остаются ASCII — это защищает от проблем с
  64-байтовым лимитом Telegram и делает маршрутизацию callback'ов
  тривиальной (`data.startswith("cat_")`).
* Для `WebApp`-кнопки используется только HTTPS-URL. Если в конфиге
  `webapp_url` пуст, кнопка не добавляется — это рабочий режим,
  когда мини-приложение ещё не развёрнуто.
"""

from __future__ import annotations

from typing import List, Mapping, Optional, Sequence

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from .localization import Localization


# --------------------------------------------------------------------------- #
# Архитектурная заметка: где открывается Mini App
# --------------------------------------------------------------------------- #
# Mini App «Книга рецептов» доступен пользователю из двух мест:
#   1. Menu Button бота (настраивается один раз в BotFather на тот же
#      HTTPS-URL, что и `WEBAPP_URL`; Telegram сам держит его в UI
#      рядом с полем ввода).
#   2. Inline-меню `/menu` — там есть WebApp-кнопка с `web_app=...`.
#
# В Reply-клавиатуре (та, что всегда видна внизу) WebApp-кнопка намеренно
# НЕ дублируется, чтобы не перегружать главный экран. Поэтому ниже в
# `main_menu_keyboard` никакого `web_app` нет, а helper-функция для
# KeyboardButton-WebApp удалена — нужен только inline-вариант.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Тексты кнопок-якорей (Reply-клавиатура)
# --------------------------------------------------------------------------- #
# Эти же строки используются в `handlers/messages.py` для маршрутизации
# текстовых сообщений — поэтому вынесены в константы.

BTN_MENU: str = "📋 Меню"
BTN_SAVED_RECIPES: str = "📚 Сохраненные рецепты"
BTN_HELP: str = "ℹ️ Помощь"
BTN_FEEDBACK: str = "✉️ Обратная связь"
BTN_WEBAPP: str = "📖 Книга рецептов"


# --------------------------------------------------------------------------- #
# Вспомогательные фабрики кнопок
# --------------------------------------------------------------------------- #

def _is_valid_webapp_url(url: str) -> bool:
    """Проверить, что URL подходит для `WebAppInfo`.

    Telegram разрешает WebApp только поверх HTTPS. Пустые значения и
    локальные/HTTP-ссылки молча игнорируем — так код остаётся безопасным
    для dev-окружения, где мини-приложение ещё не задеплоено.
    """
    if not url:
        return False
    cleaned = url.strip().lower()
    return cleaned.startswith("https://")


def webapp_button(
    webapp_url: str,
    label: str = BTN_WEBAPP,
) -> Optional[InlineKeyboardButton]:
    """Вернуть `InlineKeyboardButton`-WebApp для заданного URL.

    Используется только в :func:`menu_keyboard` — Reply-клавиатура
    сознательно не содержит WebApp-кнопок (см. архитектурную заметку
    в шапке модуля).

    Args:
        webapp_url: Адрес мини-приложения (должен быть HTTPS).
        label: Текст кнопки (по умолчанию «📖 Книга рецептов»).

    Returns:
        Готовая кнопка либо ``None``, если `webapp_url` пустой / не HTTPS.
    """
    if not _is_valid_webapp_url(webapp_url):
        return None
    return InlineKeyboardButton(label, web_app=WebAppInfo(url=webapp_url))


# --------------------------------------------------------------------------- #
# Reply-клавиатуры
# --------------------------------------------------------------------------- #

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главная Reply-клавиатура, приветствующая пользователя.

    Содержит единственную кнопку «📋 Меню». Mini App «📖 Книга
    рецептов» сюда сознательно не добавляется — он открывается через
    Menu Button бота (BotFather) и через inline-меню `/menu`, чтобы не
    дублировать входную точку и не загромождать всегда видимую
    клавиатуру.
    """
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_MENU)]],
        resize_keyboard=True,
        is_persistent=True,
    )


# --------------------------------------------------------------------------- #
# Inline-клавиатуры
# --------------------------------------------------------------------------- #

def menu_keyboard(webapp_url: str = "") -> InlineKeyboardMarkup:
    """Inline-меню (открывается по команде /menu или кнопке «📋 Меню»).

    Если задан `webapp_url`, над основными кнопками появляется
    WebApp-кнопка «📖 Книга рецептов».

    Args:
        webapp_url: URL развёрнутого мини-приложения (HTTPS).
            Пустая строка / не-HTTPS → кнопка WebApp не добавляется.
    """
    rows: List[List[InlineKeyboardButton]] = []

    web_btn = webapp_button(webapp_url)
    if web_btn is not None:
        rows.append([web_btn])

    rows.append([
        InlineKeyboardButton(BTN_SAVED_RECIPES, callback_data="show_categories"),
        InlineKeyboardButton(BTN_HELP, callback_data="show_help"),
    ])
    rows.append([
        InlineKeyboardButton(BTN_FEEDBACK, callback_data="feedback"),
    ])
    return InlineKeyboardMarkup(rows)


def save_recipe_keyboard() -> InlineKeyboardMarkup:
    """Кнопки «✅ Сохранить» / «❌ Не сохранять» под распарсенным рецептом."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Сохранить", callback_data="save"),
            InlineKeyboardButton("❌ Не сохранять", callback_data="dont_save"),
        ],
    ])


def categories_keyboard(
    loc: Localization,
    categories: Sequence[Mapping[str, object]],
) -> InlineKeyboardMarkup:
    """Клавиатура со списком категорий пользователя.

    Каждая строка — одна категория: «{эмодзи} {локализованное имя} ({count})»,
    `callback_data="cat_{ключ}"`. Внизу — «◀️ Назад в меню».

    Args:
        loc: Локализатор для перевода ключей в отображаемые имена.
        categories: Результат `SupabaseStorage.get_categories(...)` —
            список словарей ``{"key": ..., "count": ...}``.
    """
    rows: List[List[InlineKeyboardButton]] = []

    for cat in categories:
        key = str(cat.get("key", "other"))
        count = int(cat.get("count", 0) or 0)
        label = f"{loc.get_meal_type_emoji(key)} {loc.get_meal_type_name(key)} ({count})"
        rows.append([InlineKeyboardButton(label, callback_data=f"cat_{key}")])

    rows.append([InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(rows)


def recipes_list_keyboard(
    recipes: Sequence[Mapping[str, object]],
) -> InlineKeyboardMarkup:
    """Клавиатура со списком рецептов в выбранной категории.

    На каждой кнопке — название рецепта; `callback_data="view_{id}"`.
    Длинные заголовки обрезаются до 40 символов. Внизу — «◀️ Назад».
    """
    rows: List[List[InlineKeyboardButton]] = []

    for recipe in recipes:
        title = str(recipe.get("title") or "Без названия").strip()
        if len(title) > 40:
            title = title[:39] + "…"
        recipe_id = str(recipe.get("id") or "")
        if not recipe_id:
            continue
        rows.append([
            InlineKeyboardButton(title, callback_data=f"view_{recipe_id}")
        ])

    rows.append([
        InlineKeyboardButton("◀️ Назад", callback_data="back_to_categories")
    ])
    return InlineKeyboardMarkup(rows)


def recipe_detail_keyboard(recipe_id: str) -> InlineKeyboardMarkup:
    """Кнопки под детальным просмотром рецепта: «🗑 Удалить» и «◀️ Назад»."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{recipe_id}"),
            InlineKeyboardButton("◀️ Назад", callback_data="back_to_categories"),
        ],
    ])


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Одна кнопка «◀️ Назад в меню» — используется, например, в экране помощи."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")],
    ])
