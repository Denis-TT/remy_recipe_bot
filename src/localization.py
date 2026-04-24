"""
Модуль локализации Remy Bot.

Отвечает за две задачи:

1. **Нормализация** пользовательских/AI-значений ключевых полей рецепта
   (`meal_type`, `difficulty`, `cuisine`) — приведение любого входного
   значения (русское, английское, верхний/нижний регистр, множественное
   число) к строгому латинскому ключу.

2. **Локализация** — перевод этих латинских ключей в русские названия
   с эмодзи, готовые для отображения в Telegram.

Класс `Localization` спроектирован так, чтобы в дальнейшем легко
добавить новые языки: достаточно расширить словарь `TRANSLATIONS`
и при создании экземпляра передать нужный `language`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping


class Localization:
    """Локализация и нормализация ключевых полей рецепта.

    Класс объединяет два слоя:

    * *Статический* — методы нормализации (`normalize_meal_type`,
      `normalize_difficulty`, `normalize_cuisine`, `normalize_recipe`).
      Они не зависят от языка и всегда возвращают латинский ключ.

    * *Экземплярный* — методы отображения (`translate`, `get_*_name`,
      `get_*_emoji`, `get_*_display`). Они используют язык,
      переданный в конструктор.

    Пример:
        >>> loc = Localization("ru")
        >>> Localization.normalize_meal_type("Обед")
        'lunch'
        >>> loc.get_meal_type_display("lunch")
        '🍲 Обеды'
    """

    # --------------------------------------------------------------------- #
    # Валидные ключи
    # --------------------------------------------------------------------- #

    VALID_MEAL_TYPES: List[str] = [
        "breakfast", "lunch", "dinner", "dessert",
        "snack", "salad", "soup", "baking", "drink", "other",
    ]

    VALID_DIFFICULTY: List[str] = ["easy", "medium", "hard"]

    # --------------------------------------------------------------------- #
    # Словарь переводов
    # --------------------------------------------------------------------- #

    TRANSLATIONS: Dict[str, Dict[str, str]] = {
        "ru": {
            # Категории блюд
            "meal_type_breakfast": "Завтраки",
            "meal_type_lunch": "Обеды",
            "meal_type_dinner": "Ужины",
            "meal_type_dessert": "Десерты",
            "meal_type_snack": "Перекусы",
            "meal_type_salad": "Салаты",
            "meal_type_soup": "Супы",
            "meal_type_baking": "Выпечка",
            "meal_type_drink": "Напитки",
            "meal_type_other": "Другое",

            # Сложность
            "difficulty_easy": "Легко",
            "difficulty_medium": "Средне",
            "difficulty_hard": "Сложно",

            # Кухни
            "cuisine_italian": "Итальянская",
            "cuisine_russian": "Русская",
            "cuisine_japanese": "Японская",
            "cuisine_french": "Французская",
            "cuisine_chinese": "Китайская",
            "cuisine_georgian": "Грузинская",
            "cuisine_korean": "Корейская",
            "cuisine_indian": "Индийская",
            "cuisine_thai": "Тайская",
            "cuisine_mexican": "Мексиканская",
            "cuisine_mediterranean": "Средиземноморская",
            "cuisine_american": "Американская",
            "cuisine_european": "Европейская",
            "cuisine_asian": "Азиатская",
            "cuisine_other": "Другая",
        },
    }

    # --------------------------------------------------------------------- #
    # Эмодзи
    # --------------------------------------------------------------------- #

    MEAL_TYPE_EMOJIS: Dict[str, str] = {
        "breakfast": "🍳",
        "lunch": "🍲",
        "dinner": "🍽️",
        "dessert": "🍰",
        "snack": "🥨",
        "salad": "🥗",
        "soup": "🥣",
        "baking": "🧁",
        "drink": "🥤",
        "other": "📦",
    }

    DIFFICULTY_EMOJIS: Dict[str, str] = {
        "easy": "🟢",
        "medium": "🟡",
        "hard": "🔴",
    }

    # --------------------------------------------------------------------- #
    # Таблицы алиасов для нормализации
    # --------------------------------------------------------------------- #
    # Ключи хранятся в нижнем регистре и без лишних пробелов.
    # Значения — строго из VALID_* списков.

    _MEAL_TYPE_ALIASES: Dict[str, str] = {
        # Русский — единственное число и падежные формы
        "завтрак": "breakfast",
        "обед": "lunch",
        "ужин": "dinner",
        "десерт": "dessert",
        "перекус": "snack",
        "закуска": "snack",
        "салат": "salad",
        "суп": "soup",
        "выпечка": "baking",
        "напиток": "drink",
        "другое": "other",
        "основное блюдо": "lunch",
        "основное": "lunch",
        "горячее": "lunch",
        "второе": "lunch",
        "первое": "soup",

        # Русский — множественное число
        "завтраки": "breakfast",
        "обеды": "lunch",
        "ужины": "dinner",
        "десерты": "dessert",
        "перекусы": "snack",
        "закуски": "snack",
        "салаты": "salad",
        "супы": "soup",
        "напитки": "drink",

        # Английский — единственное число
        "breakfast": "breakfast",
        "lunch": "lunch",
        "dinner": "dinner",
        "dessert": "dessert",
        "snack": "snack",
        "appetizer": "snack",
        "starter": "snack",
        "salad": "salad",
        "soup": "soup",
        "baking": "baking",
        "bakery": "baking",
        "drink": "drink",
        "beverage": "drink",
        "other": "other",
        "main": "lunch",
        "main course": "lunch",
        "main dish": "lunch",

        # Английский — множественное число
        "breakfasts": "breakfast",
        "lunches": "lunch",
        "dinners": "dinner",
        "desserts": "dessert",
        "snacks": "snack",
        "appetizers": "snack",
        "starters": "snack",
        "salads": "salad",
        "soups": "soup",
        "drinks": "drink",
        "beverages": "drink",
    }

    _DIFFICULTY_ALIASES: Dict[str, str] = {
        # easy
        "легко": "easy",
        "просто": "easy",
        "лёгкий": "easy",
        "легкий": "easy",
        "лёгкая": "easy",
        "легкая": "easy",
        "простой": "easy",
        "простая": "easy",
        "easy": "easy",
        "beginner": "easy",
        "simple": "easy",

        # medium
        "средне": "medium",
        "нормально": "medium",
        "средний": "medium",
        "средняя": "medium",
        "medium": "medium",
        "normal": "medium",
        "intermediate": "medium",
        "moderate": "medium",

        # hard
        "сложно": "hard",
        "тяжело": "hard",
        "сложный": "hard",
        "сложная": "hard",
        "тяжёлый": "hard",
        "тяжелый": "hard",
        "тяжёлая": "hard",
        "тяжелая": "hard",
        "hard": "hard",
        "difficult": "hard",
        "advanced": "hard",
        "complex": "hard",
    }

    _CUISINE_ALIASES: Dict[str, str] = {
        # Английский — каноничная форма
        "italian": "italian",
        "russian": "russian",
        "japanese": "japanese",
        "french": "french",
        "chinese": "chinese",
        "georgian": "georgian",
        "korean": "korean",
        "indian": "indian",
        "thai": "thai",
        "mexican": "mexican",
        "mediterranean": "mediterranean",
        "american": "american",
        "european": "european",
        "asian": "asian",
        "other": "other",

        # Русский — мужской и женский род + падежные вариации
        "итальянская": "italian",
        "итальянский": "italian",
        "итальянское": "italian",
        "русская": "russian",
        "русский": "russian",
        "русское": "russian",
        "японская": "japanese",
        "японский": "japanese",
        "японское": "japanese",
        "французская": "french",
        "французский": "french",
        "французское": "french",
        "китайская": "chinese",
        "китайский": "chinese",
        "китайское": "chinese",
        "грузинская": "georgian",
        "грузинский": "georgian",
        "грузинское": "georgian",
        "корейская": "korean",
        "корейский": "korean",
        "корейское": "korean",
        "индийская": "indian",
        "индийский": "indian",
        "индийское": "indian",
        "тайская": "thai",
        "тайский": "thai",
        "тайское": "thai",
        "мексиканская": "mexican",
        "мексиканский": "mexican",
        "мексиканское": "mexican",
        "средиземноморская": "mediterranean",
        "средиземноморский": "mediterranean",
        "средиземноморское": "mediterranean",
        "американская": "american",
        "американский": "american",
        "американское": "american",
        "европейская": "european",
        "европейский": "european",
        "европейское": "european",
        "азиатская": "asian",
        "азиатский": "asian",
        "азиатское": "asian",
        "другая": "other",
        "другой": "other",
        "другое": "other",
    }

    # --------------------------------------------------------------------- #
    # Конструктор
    # --------------------------------------------------------------------- #

    def __init__(self, language: str = "ru") -> None:
        """Создать локализатор для указанного языка.

        Args:
            language: Код языка, например `"ru"`. Если запрошенный язык
                отсутствует в `TRANSLATIONS`, методы отображения будут
                возвращать исходные ключи (safe fallback).
        """
        self.language: str = language

    # --------------------------------------------------------------------- #
    # Внутренние утилиты
    # --------------------------------------------------------------------- #

    @staticmethod
    def _clean(value: Any) -> str:
        """Привести значение к «канонической» форме для поиска в алиасах.

        Приводит к строке, обрезает пробелы, переводит в нижний регистр
        и нормализует букву «ё» → «е» (для устойчивости русских
        словарей). Для `None` и пустых значений возвращает пустую строку.

        Args:
            value: Произвольное значение.

        Returns:
            Очищенная строка в нижнем регистре без пробелов по краям.
        """
        if value is None:
            return ""
        text = str(value).strip().lower()
        return text.replace("ё", "е")

    # --------------------------------------------------------------------- #
    # Методы нормализации (статические)
    # --------------------------------------------------------------------- #

    @staticmethod
    def normalize_meal_type(value: Any) -> str:
        """Нормализовать тип блюда в латиницу.

        Принимает любое значение (русское, английское, смешанное,
        в единственном или множественном числе, в любом регистре)
        и возвращает строго один из `VALID_MEAL_TYPES`.
        Значение по умолчанию — ``"other"``.

        Примеры:
            >>> Localization.normalize_meal_type("обед")
            'lunch'
            >>> Localization.normalize_meal_type("Dinner")
            'dinner'
            >>> Localization.normalize_meal_type("супы")
            'soup'
            >>> Localization.normalize_meal_type(None)
            'other'
            >>> Localization.normalize_meal_type("неизвестное")
            'other'

        Args:
            value: Исходное значение типа блюда любого типа.

        Returns:
            Нормализованный ключ (всегда латиница, всегда из
            `VALID_MEAL_TYPES`).
        """
        cleaned = Localization._clean(value)
        if not cleaned:
            return "other"

        normalized = Localization._MEAL_TYPE_ALIASES.get(cleaned)
        if normalized is not None:
            return normalized

        # На случай, если пришло уже каноническое значение, но с отступами
        # или в ином регистре, которого нет в таблице алиасов напрямую.
        if cleaned in Localization.VALID_MEAL_TYPES:
            return cleaned

        return "other"

    @staticmethod
    def normalize_difficulty(value: Any) -> str:
        """Нормализовать уровень сложности в латиницу.

        Принимает любое значение и возвращает строго один из
        `VALID_DIFFICULTY`. Значение по умолчанию — ``"medium"``.

        Примеры:
            >>> Localization.normalize_difficulty("легко")
            'easy'
            >>> Localization.normalize_difficulty("Medium")
            'medium'
            >>> Localization.normalize_difficulty("тяжело")
            'hard'
            >>> Localization.normalize_difficulty(None)
            'medium'

        Args:
            value: Исходное значение сложности.

        Returns:
            Один из ``"easy"``, ``"medium"``, ``"hard"``.
        """
        cleaned = Localization._clean(value)
        if not cleaned:
            return "medium"

        normalized = Localization._DIFFICULTY_ALIASES.get(cleaned)
        if normalized is not None:
            return normalized

        if cleaned in Localization.VALID_DIFFICULTY:
            return cleaned

        return "medium"

    @staticmethod
    def normalize_cuisine(value: Any) -> str:
        """Нормализовать кухню в латиницу.

        Поддерживает все кухни из словаря переводов и их русские
        аналоги (мужской, женский, средний род). Если значение не
        найдено в таблице алиасов — возвращает исходную строку в
        нижнем регистре без ведущих/концевых пробелов. Для `None`
        и пустой строки — ``"other"``.

        Примеры:
            >>> Localization.normalize_cuisine("итальянская")
            'italian'
            >>> Localization.normalize_cuisine("Italian")
            'italian'
            >>> Localization.normalize_cuisine("vietnamese")
            'vietnamese'
            >>> Localization.normalize_cuisine(None)
            'other'

        Args:
            value: Исходное значение кухни.

        Returns:
            Нормализованный ключ кухни в нижнем регистре.
        """
        cleaned = Localization._clean(value)
        if not cleaned:
            return "other"

        normalized = Localization._CUISINE_ALIASES.get(cleaned)
        if normalized is not None:
            return normalized

        return cleaned

    @staticmethod
    def normalize_recipe(recipe: Mapping[str, Any]) -> Dict[str, Any]:
        """Нормализовать ключевые поля рецепта.

        Возвращает **новый** словарь — копию входного `recipe`,
        в котором поля `meal_type`, `difficulty` и `cuisine` приведены
        к каноническим ключам. Остальные поля не изменяются.
        Если какого-то из трёх полей нет в исходном словаре, оно
        будет добавлено со значением по умолчанию: ``"other"``,
        ``"medium"`` и ``"other"`` соответственно.

        Примеры:
            >>> Localization.normalize_recipe(
            ...     {"meal_type": "обед", "difficulty": "сложно",
            ...      "cuisine": "японская", "title": "Рамен"}
            ... ) == {"meal_type": "lunch", "difficulty": "hard",
            ...      "cuisine": "japanese", "title": "Рамен"}
            True

        Args:
            recipe: Словарь рецепта (например, разобранный JSON от AI).

        Returns:
            Новый словарь с нормализованными `meal_type`, `difficulty`,
            `cuisine` и неизменёнными прочими полями.
        """
        result: Dict[str, Any] = dict(recipe)
        result["meal_type"] = Localization.normalize_meal_type(result.get("meal_type"))
        result["difficulty"] = Localization.normalize_difficulty(result.get("difficulty"))
        result["cuisine"] = Localization.normalize_cuisine(result.get("cuisine"))
        return result

    # --------------------------------------------------------------------- #
    # Методы отображения (экземпляра)
    # --------------------------------------------------------------------- #

    def translate(self, key: str, category: str = "") -> str:
        """Перевести ключ в человеческое название для текущего языка.

        Полный ключ формируется как ``"{category}_{key}"`` (если
        `category` не пустая). Если перевод не найден — возвращается
        исходный `key` без изменений (безопасный fallback).

        Примеры:
            >>> loc = Localization("ru")
            >>> loc.translate("italian", "cuisine")
            'Итальянская'
            >>> loc.translate("unknown", "cuisine")
            'unknown'

        Args:
            key: Базовый ключ (например, ``"italian"``).
            category: Категория-префикс (``"meal_type"``, ``"difficulty"``,
                ``"cuisine"``). При пустом значении ключ ищется как есть.

        Returns:
            Локализованная строка или исходный `key`, если перевода нет.
        """
        full_key = f"{category}_{key}" if category else key
        translations = self.TRANSLATIONS.get(self.language, {})
        return translations.get(full_key, key)

    def get_meal_type_name(self, key: str) -> str:
        """Вернуть локализованное название типа блюда.

        Примеры:
            >>> Localization("ru").get_meal_type_name("lunch")
            'Обеды'
        """
        return self.translate(key, "meal_type")

    def get_meal_type_emoji(self, key: str) -> str:
        """Вернуть эмодзи для типа блюда.

        Для неизвестных ключей возвращает эмодзи ``other`` (``📦``).

        Примеры:
            >>> Localization("ru").get_meal_type_emoji("lunch")
            '🍲'
        """
        return self.MEAL_TYPE_EMOJIS.get(key, self.MEAL_TYPE_EMOJIS["other"])

    def get_meal_type_display(self, key: str) -> str:
        """Вернуть строку ``"<эмодзи> <название>"`` для типа блюда.

        Примеры:
            >>> Localization("ru").get_meal_type_display("lunch")
            '🍲 Обеды'
        """
        return f"{self.get_meal_type_emoji(key)} {self.get_meal_type_name(key)}"

    def get_difficulty_name(self, key: str) -> str:
        """Вернуть локализованное название сложности.

        Примеры:
            >>> Localization("ru").get_difficulty_name("medium")
            'Средне'
        """
        return self.translate(key, "difficulty")

    def get_difficulty_emoji(self, key: str) -> str:
        """Вернуть эмодзи для уровня сложности.

        Для неизвестных ключей возвращает эмодзи ``medium`` (``🟡``).

        Примеры:
            >>> Localization("ru").get_difficulty_emoji("hard")
            '🔴'
        """
        return self.DIFFICULTY_EMOJIS.get(key, self.DIFFICULTY_EMOJIS["medium"])

    def get_difficulty_display(self, key: str) -> str:
        """Вернуть строку ``"<эмодзи> <название>"`` для сложности.

        Примеры:
            >>> Localization("ru").get_difficulty_display("medium")
            '🟡 Средне'
        """
        return f"{self.get_difficulty_emoji(key)} {self.get_difficulty_name(key)}"

    def get_cuisine_name(self, key: str) -> str:
        """Вернуть локализованное название кухни.

        Для неизвестных кухонь (не попавших в словарь переводов)
        возвращает исходный ключ.

        Примеры:
            >>> Localization("ru").get_cuisine_name("italian")
            'Итальянская'
            >>> Localization("ru").get_cuisine_name("vietnamese")
            'vietnamese'
        """
        return self.translate(key, "cuisine")


# --------------------------------------------------------------------------- #
# Встроенные тесты
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    loc = Localization("ru")

    # --- Нормализация meal_type -----------------------------------------
    assert Localization.normalize_meal_type("обед") == "lunch"
    assert Localization.normalize_meal_type("Dinner") == "dinner"
    assert Localization.normalize_meal_type(None) == "other"
    assert Localization.normalize_meal_type("неизвестное") == "other"
    assert Localization.normalize_meal_type("") == "other"
    assert Localization.normalize_meal_type("ЗАВТРАК") == "breakfast"
    assert Localization.normalize_meal_type("lunches") == "lunch"
    assert Localization.normalize_meal_type("закуски") == "snack"
    assert Localization.normalize_meal_type("первое") == "soup"
    assert Localization.normalize_meal_type("горячее") == "lunch"
    assert Localization.normalize_meal_type(123) == "other"

    # --- Нормализация difficulty ----------------------------------------
    assert Localization.normalize_difficulty("легко") == "easy"
    assert Localization.normalize_difficulty("Medium") == "medium"
    assert Localization.normalize_difficulty("advanced") == "hard"
    assert Localization.normalize_difficulty(None) == "medium"
    assert Localization.normalize_difficulty("бред") == "medium"
    assert Localization.normalize_difficulty("ТЯЖЕЛО") == "hard"

    # --- Нормализация cuisine -------------------------------------------
    assert Localization.normalize_cuisine("итальянская") == "italian"
    assert Localization.normalize_cuisine("Italian") == "italian"
    assert Localization.normalize_cuisine("ЯПОНСКАЯ") == "japanese"
    assert Localization.normalize_cuisine("vietnamese") == "vietnamese"
    assert Localization.normalize_cuisine(None) == "other"

    # --- Нормализация целого рецепта ------------------------------------
    recipe = {"meal_type": "обед", "difficulty": "сложно", "cuisine": "японская"}
    normalized = Localization.normalize_recipe(recipe)
    assert normalized["meal_type"] == "lunch"
    assert normalized["difficulty"] == "hard"
    assert normalized["cuisine"] == "japanese"
    assert recipe["meal_type"] == "обед", "normalize_recipe не должна мутировать исходный dict"

    # Отсутствующие поля → значения по умолчанию
    defaults = Localization.normalize_recipe({"title": "Борщ"})
    assert defaults["meal_type"] == "other"
    assert defaults["difficulty"] == "medium"
    assert defaults["cuisine"] == "other"
    assert defaults["title"] == "Борщ"

    # --- Отображение ----------------------------------------------------
    assert loc.get_meal_type_display("lunch") == "🍲 Обеды"
    assert loc.get_difficulty_display("medium") == "🟡 Средне"
    assert loc.get_cuisine_name("italian") == "Итальянская"
    assert loc.get_meal_type_name("dessert") == "Десерты"
    assert loc.get_meal_type_emoji("breakfast") == "🍳"
    assert loc.get_difficulty_name("hard") == "Сложно"

    # Неизвестный ключ перевода → возвращаем исходный ключ
    assert loc.translate("banana", "cuisine") == "banana"
    assert loc.get_cuisine_name("vietnamese") == "vietnamese"

    # Безопасный fallback для неизвестного языка
    loc_xx = Localization("xx")
    assert loc_xx.get_meal_type_name("lunch") == "lunch"

    print("✅ Все тесты локализации пройдены!")
