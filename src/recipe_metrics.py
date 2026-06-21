"""
Метрики рецепта: форматирование времени, оценка порций (USDA RACC).

Время хранится в минутах:
* prep_time + cook_time — активная работа «у плиты»;
* total_time — календарное время до подачи (включая расстойку, маринад и т. п.).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence

# Эталонные порции (USDA 21 CFR §101.12 RACC, адаптация для домашней кухни).
_RACC_GRAMS_BY_DISH: dict[str, int] = {
    "soup": 245,
    "main": 250,
    "side": 140,
    "salad": 120,
    "appetizer": 85,
    "dessert": 120,
    "drink": 250,
    "baking": 75,
    "sauce": 30,
    "preserve": 50,
}

_RACC_GRAMS_BY_MEAL: dict[str, int] = {
    "soup": 245,
    "snack": 85,
    "salad": 120,
    "baking": 75,
    "drink": 250,
    "dessert": 120,
}

_SERVINGS_IN_SOURCE_RE = re.compile(
    r"(?:"
    r"(?:на|для)\s*(\d{1,2})\s*(?:порц|перс|чел|человек|persons?|servings?)"
    r"|(\d{1,2})\s*(?:порц(?:ий|ии|ия)?|servings?|persons?)"
    r")",
    re.IGNORECASE,
)

_GENERIC_NUTRITION_NOTE_RE = re.compile(
    r"калорийност\w*\s+и\s+бжу\s+оцен",
    re.IGNORECASE,
)


def format_duration_minutes(minutes: int) -> str:
    """Форматировать минуты: ``70`` → ``1 ч 10 мин``, ``4320`` → ``3 сут``."""
    total = max(0, int(minutes))
    if total == 0:
        return ""

    if total < 60:
        return f"{total} мин"

    days, rem = divmod(total, 1440)
    hours, mins = divmod(rem, 60)

    parts: List[str] = []
    if days:
        parts.append(f"{days} сут")
    if hours:
        parts.append(f"{hours} ч")
    if mins and (not days or mins > 0):
        parts.append(f"{mins} мин")
    return " ".join(parts)


def normalize_recipe_times(
    prep_time: int,
    cook_time: int,
    total_time: int,
) -> tuple[int, int, int]:
    """Согласовать prep/cook/total: total не меньше активного времени."""
    prep = max(0, int(prep_time))
    cook = max(0, int(cook_time))
    active = prep + cook
    total = max(0, int(total_time))
    if total <= 0:
        total = active
    elif total < active:
        total = active
    return prep, cook, total


def active_time_minutes(prep_time: int, cook_time: int) -> int:
    return max(0, int(prep_time)) + max(0, int(cook_time))


def format_recipe_time_lines(
    prep_time: int,
    cook_time: int,
    total_time: int,
    *,
    html: bool = False,
) -> List[str]:
    """Строки для карточки: общее время и время «у плиты», если они различаются."""
    prep, cook, total = normalize_recipe_times(prep_time, cook_time, total_time)
    active = active_time_minutes(prep, cook)
    total_label = format_duration_minutes(total)
    if not total_label:
        return []

    active_label = format_duration_minutes(active)
    if total > active and active > 0:
        if html:
            return [
                f"⏱ Общее: {total_label}",
                f"👨‍🍳 У плиты: {active_label}",
            ]
        return [
            f"⏱ Общее: {total_label}",
            f"👨‍🍳 У плиты: {active_label}",
        ]

    prefix = "⏱ "
    return [f"{prefix}{total_label}"]


def strip_redundant_nutrition_note(note: str, *, estimated: bool) -> str:
    """Убрать шаблонные пояснения — для оценки достаточно префикса ``~``."""
    text = (note or "").strip()
    if not text:
        return ""
    if estimated and _GENERIC_NUTRITION_NOTE_RE.search(text):
        return ""
    if estimated and "типичн" in text.lower() and "ингредиент" in text.lower():
        return ""
    return text


def extract_servings_from_text(text: str) -> Optional[int]:
    """Явное число порций из исходного текста («на 6 порций»)."""
    for match in _SERVINGS_IN_SOURCE_RE.finditer(text or ""):
        raw = match.group(1) or match.group(2)
        if raw:
            n = int(raw)
            if 1 <= n <= 24:
                return n
    return None


def _ingredient_mass_grams(amount: float, unit: str) -> Optional[float]:
    if amount <= 0:
        return None
    u = (unit or "").lower().strip().replace(" ", "")
    if u in {"г", "гр", "gram", "grams"}:
        return amount
    if u in {"кг", "kg"}:
        return amount * 1000
    if u in {"мл", "ml"}:
        return amount
    if u in {"л", "l"}:
        return amount * 1000
    if u in {"ст.л.", "стл", "tbsp"}:
        return amount * 15
    if u in {"ч.л.", "чл", "tsp"}:
        return amount * 5
    return None


def _racc_grams(dish_type: str, meal_type: str) -> int:
    meal = (meal_type or "").strip().lower()
    if meal in _RACC_GRAMS_BY_MEAL:
        return _RACC_GRAMS_BY_MEAL[meal]
    dish = (dish_type or "main").strip().lower()
    return _RACC_GRAMS_BY_DISH.get(dish, 250)


def _total_recipe_mass_grams(ingredients: Sequence[Mapping[str, Any]]) -> tuple[float, float]:
    """(сумма в г, доля ингредиентов с известной массой 0..1)."""
    total = 0.0
    known = 0
    countable = 0
    for raw in ingredients:
        if not isinstance(raw, Mapping):
            continue
        amount = raw.get("amount")
        try:
            amt = float(amount) if amount is not None else 0.0
        except (TypeError, ValueError):
            amt = 0.0
        unit = str(raw.get("unit") or "")
        notes = str(raw.get("notes") or "").lower()
        if amt <= 0 or "по вкусу" in notes or "на глаз" in notes:
            continue
        countable += 1
        grams = _ingredient_mass_grams(amt, unit)
        if grams is not None:
            total += grams
            known += 1
    coverage = (known / countable) if countable else 0.0
    return total, coverage


def refine_servings(
    ingredients: Iterable[Mapping[str, Any]],
    *,
    dish_type: str,
    meal_type: str,
    raw_text: str,
    ai_servings: int,
) -> int:
    """Скорректировать порции по весу и типу блюда, не ломая явные указания в источнике."""
    explicit = extract_servings_from_text(raw_text)
    if explicit is not None:
        return explicit

    current = max(1, int(ai_servings) if ai_servings else 4)
    ing_list = [i for i in ingredients if isinstance(i, Mapping)]
    total_g, coverage = _total_recipe_mass_grams(ing_list)
    if total_g < 150 or coverage < 0.45:
        return current

    racc = _racc_grams(dish_type, meal_type)
    estimated = int(round(total_g / racc))
    estimated = max(1, min(24, estimated))

    if current == 4 and estimated != 4:
        return estimated
    if abs(estimated - current) >= max(2, int(current * 0.35)):
        return estimated
    return current


if __name__ == "__main__":
    assert format_duration_minutes(45) == "45 мин"
    assert format_duration_minutes(70) == "1 ч 10 мин"
    assert format_duration_minutes(120) == "2 ч"
    assert format_duration_minutes(4320) == "3 сут"
    assert format_duration_minutes(4350) == "3 сут 30 мин"
    assert normalize_recipe_times(30, 15, 4320) == (30, 15, 4320)
    assert normalize_recipe_times(30, 15, 0) == (30, 15, 45)
    lines = format_recipe_time_lines(30, 15, 4320)
    assert len(lines) == 2 and "сут" in lines[0] and "плиты" in lines[1]
    assert strip_redundant_nutrition_note(
        "Калорийность и БЖУ оценены на основе указанных ингредиентов и типичных значений.",
        estimated=True,
    ) == ""
    assert extract_servings_from_text("Рецепт на 6 порций") == 6
    est = refine_servings(
        [{"name": "Мука", "amount": 1000, "unit": "г", "notes": ""}],
        dish_type="main",
        meal_type="lunch",
        raw_text="",
        ai_servings=4,
    )
    assert est == 4  # 1000/250 = 4
    print("✅ recipe_metrics")
