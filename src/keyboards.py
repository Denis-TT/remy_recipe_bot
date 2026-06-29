"""
Клавиатуры Remy Bot.

Содержит функции-фабрики, возвращающие готовые объекты клавиатур
Telegram. Весь код, формирующий визуальные элементы интерфейса
(Reply/Inline-клавиатуры, кнопки меню, категорий, рецепта), собран
здесь — чтобы хендлеры остались максимально тонкими и сосредоточились
на бизнес-логике.

Важные инварианты:

* `callback_data` **всегда латиница** (`dishtype_soup`, `view_<uuid>`, ...).
  Локализованный текст на кнопках берётся из `Localization`, но
  передаваемые данные остаются ASCII — это защищает от проблем с
  64-байтовым лимитом Telegram и делает маршрутизацию callback'ов
  тривиальной (`data.startswith("dishtype_")`).
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


def parse_result_keyboard(temp_id: str) -> InlineKeyboardMarkup:
    """Кнопки под распарсенным рецептом (до сохранения): шеф + сохранить."""
    tid = str(temp_id or "").strip()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨‍🍳 Спросить у шефа Реми",
                callback_data=f"chef_temp_{tid}",
            ),
        ],
        [
            InlineKeyboardButton("✅ Сохранить", callback_data=f"save_{tid}"),
            InlineKeyboardButton("❌ Не сохранять", callback_data=f"dont_save_{tid}"),
        ],
    ])


def save_recipe_keyboard(temp_id: str) -> InlineKeyboardMarkup:
    """Алиас для обратной совместимости."""
    return parse_result_keyboard(temp_id)


def dish_types_keyboard(
    loc: Localization,
    dish_types: Sequence[Mapping[str, object]],
) -> InlineKeyboardMarkup:
    """Клавиатура первого уровня: ``dish_type``.

    Каждая строка — одна категория: «{эмодзи} {локализованное имя} ({count})»,
    `callback_data="dishtype_{ключ}"`. Внизу — «◀️ Назад в меню».
    """
    rows: List[List[InlineKeyboardButton]] = []

    for item in dish_types:
        key = str(item.get("key", "main"))
        count = int(item.get("count", 0) or 0)
        label = f"{loc.get_dish_type_emoji(key)} {loc.get_dish_type_name(key)} ({count})"
        rows.append([InlineKeyboardButton(label, callback_data=f"dishtype_{key}")])

    rows.append([InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(rows)


def main_ingredients_keyboard(
    loc: Localization,
    dish_type: str,
    ingredients: Sequence[Mapping[str, object]],
) -> InlineKeyboardMarkup:
    """Клавиатура второго уровня: ``main_ingredient`` внутри ``dish_type``."""
    rows: List[List[InlineKeyboardButton]] = []
    dish_key = str(dish_type or "main")

    for item in ingredients:
        key = str(item.get("key", "other"))
        count = int(item.get("count", 0) or 0)
        label = (
            f"{loc.get_main_ingredient_emoji(key)} "
            f"{loc.get_main_ingredient_name(key)} ({count})"
        )
        rows.append([
            InlineKeyboardButton(label, callback_data=f"ingredient_{dish_key}_{key}")
        ])

    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_to_categories")])
    return InlineKeyboardMarkup(rows)


def recipes_list_keyboard(
    recipes: Sequence[Mapping[str, object]],
    dish_type: str = "",
    main_ingredient: str = "",
) -> InlineKeyboardMarkup:
    """Клавиатура со списком рецептов в выбранной категории.

    На каждой кнопке — название рецепта; `callback_data="view_{...}"`.
    Длинные заголовки обрезаются до 40 символов. Внизу — «◀️ Назад».
    """
    rows: List[List[InlineKeyboardButton]] = []
    dish_key = str(dish_type or "").strip()
    ingredient_key = str(main_ingredient or "").strip()

    for recipe in recipes:
        title = str(recipe.get("title") or "Без названия").strip()
        if len(title) > 40:
            title = title[:39] + "…"
        recipe_id = str(recipe.get("id") or "")
        if not recipe_id:
            continue
        callback_data = f"view_{recipe_id}"
        if dish_key and ingredient_key:
            callback_data = f"view_{dish_key}_{ingredient_key}_{recipe_id}"
        rows.append([
            InlineKeyboardButton(title, callback_data=callback_data)
        ])

    back_callback = "back_to_categories"
    if dish_key:
        back_callback = f"dishtype_{dish_key}"
    rows.append([
        InlineKeyboardButton("◀️ Назад", callback_data=back_callback)
    ])
    return InlineKeyboardMarkup(rows)


def recipe_detail_keyboard(
    recipe_id: str,
    dish_type: str = "",
    main_ingredient: str = "",
) -> InlineKeyboardMarkup:
    """Кнопки под детальным просмотром рецепта."""
    back_callback = "back_to_categories"
    if dish_type and main_ingredient:
        back_callback = f"ingredient_{dish_type}_{main_ingredient}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👨‍🍳 Спросить у шефа Реми",
                callback_data=f"chef_{recipe_id}",
            ),
        ],
        [
            InlineKeyboardButton("📤 Поделиться", callback_data=f"share_{recipe_id}"),
        ],
        [
            InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_{recipe_id}"),
            InlineKeyboardButton("◀️ Назад", callback_data=back_callback),
        ],
    ])


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Одна кнопка «◀️ Назад в меню» — используется, например, в экране помощи."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ Назад в меню", callback_data="back_to_menu")],
    ])


def welcome_start_keyboard() -> InlineKeyboardMarkup:
    """Кнопки приветствия /start: пример, инструкция, своя ссылка."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Протестировать пример", callback_data="run_example_test")],
        [InlineKeyboardButton("📖 Инструкция и лимиты", callback_data="show_tutorial_info")],
        [InlineKeyboardButton("🔗 Отправить свою ссылку", callback_data="prompt_own_link")],
    ])


def tutorial_back_keyboard() -> InlineKeyboardMarkup:
    """Кнопка «Назад» с экрана инструкции к приветствию."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="go_to_start")],
    ])


def chef_followup_keyboard() -> InlineKeyboardMarkup:
    """После ответа шефа: продолжить диалог или выйти из режима."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, ещё вопрос", callback_data="chef_more"),
            InlineKeyboardButton("Нет, спасибо", callback_data="chef_exit"),
        ],
    ])
