"""
Классификация свободного текста без ИИ: рецепт vs болтовня vs FAQ.

Используется в ``handle_text`` до вызова GPT-нормализатора.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TextIntent(str, Enum):
    """Намерение пользователя в текстовом сообщении."""

    RECIPE = "recipe"
    GREETING = "greeting"
    THANKS = "thanks"
    HELP = "help"
    LIMITS = "limits"
    MINI_APP = "mini_app"
    OFFTOPIC = "offtopic"
    UNKNOWN = "unknown"


# Минимальная длина, чтобы вообще рассматривать текст как рецепт.
_RECIPE_MIN_CHARS = 25

# Порог баллов для intent RECIPE.
_RECIPE_SCORE_THRESHOLD = 4

_RECIPE_SECTION_RE = re.compile(
    r"(?:"
    r"ингредиент|состав|понадобится|понадобятся|"
    r"приготовлен|приготовление|способ приготовления|"
    r"готовк|рецепт"
    r")",
    re.IGNORECASE,
)

_UNIT_RE = re.compile(
    r"\d+\s*(?:г|кг|мл|л|шт|ст\.?\s*л\.?|ч\.?\s*л\.?|мг)\b",
    re.IGNORECASE,
)

_NUMBERED_STEP_RE = re.compile(r"(?:^|\n)\s*\d+[\.\):\-]\s", re.MULTILINE)

_BULLET_LINE_RE = re.compile(r"^[-•—*]\s", re.MULTILINE)

_GREETING_RE = re.compile(
    r"^(?:"
    r"привет|здравствуй|здравствуйте|добрый\s+(?:день|вечер|утро)|"
    r"hi|hello|hey|салют|хай"
    r")\b",
    re.IGNORECASE,
)

_THANKS_RE = re.compile(
    r"(?:"
    r"спасиб|благодар|thank\s*you|thanks|"
    r"класс|супер|отлично|круто|здорово"
    r")",
    re.IGNORECASE,
)

_HELP_RE = re.compile(
    r"(?:"
    r"как\s+(?:пользов|работа|отправ|сохран)|"
    r"что\s+ты\s+умеешь|что\s+умеешь|"
    r"помощ|инструк|как\s+это\s+работает|"
    r"что\s+делать|с\s+чего\s+начать"
    r")",
    re.IGNORECASE,
)

_LIMITS_RE = re.compile(
    r"(?:"
    r"лимит|ограничен|сколько\s+раз|как\s+часто|"
    r"почему\s+не\s+отвеч|долго\s+дума"
    r")",
    re.IGNORECASE,
)

_MINI_APP_RE = re.compile(
    r"(?:"
    r"книга\s+рецепт|мини[\s-]?апп|mini\s*app|"
    r"где\s+мои\s+рецепт|сохранённ"
    r")",
    re.IGNORECASE,
)

_OFFTOPIC_RE = re.compile(
    r"(?:"
    r"погод|политик|новост|курс\s+доллар|биткоин|"
    r"футбол|кино|сериал|игр[аы]\s+(?:в|на)"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TextClassification:
    """Результат классификации."""

    intent: TextIntent
    recipe_score: int = 0


def score_recipe_text(text: str) -> int:
    """Набрать баллы «похоже на рецепт» (0 = точно нет)."""
    t = (text or "").strip()
    if len(t) < _RECIPE_MIN_CHARS:
        return 0

    score = 0
    if _RECIPE_SECTION_RE.search(t):
        score += 3
    if _UNIT_RE.search(t):
        score += 2
    if _NUMBERED_STEP_RE.search(t):
        score += 2

    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    bullet_lines = sum(1 for ln in lines if _BULLET_LINE_RE.search(ln))
    if bullet_lines >= 2:
        score += 2
    elif bullet_lines == 1:
        score += 1

    if len(lines) >= 5:
        score += 1

    # Длинный текст — слабый сигнал только вместе с другими признаками.
    if len(t) >= 120 and score >= 2:
        score += 1

    return score


def classify_user_text(text: str) -> TextClassification:
    """
    Определить намерение без ИИ.

    Приоритет: явный рецепт → chitchat-паттерны → unknown.
    """
    t = (text or "").strip()
    recipe_score = score_recipe_text(t)

    if recipe_score >= _RECIPE_SCORE_THRESHOLD:
        return TextClassification(TextIntent.RECIPE, recipe_score)

    # Короткие приветствия / благодарности — не рецепт.
    if len(t) <= 80 and _GREETING_RE.search(t):
        return TextClassification(TextIntent.GREETING, recipe_score)

    if len(t) <= 100 and _THANKS_RE.search(t) and recipe_score < 2:
        return TextClassification(TextIntent.THANKS, recipe_score)

    if _HELP_RE.search(t):
        return TextClassification(TextIntent.HELP, recipe_score)

    if _LIMITS_RE.search(t):
        return TextClassification(TextIntent.LIMITS, recipe_score)

    if _MINI_APP_RE.search(t):
        return TextClassification(TextIntent.MINI_APP, recipe_score)

    if _OFFTOPIC_RE.search(t) and recipe_score < 2:
        return TextClassification(TextIntent.OFFTOPIC, recipe_score)

    return TextClassification(TextIntent.UNKNOWN, recipe_score)


def is_recipe_text(text: str) -> bool:
    """Совместимость с прежней эвристикой ``_looks_like_recipe_text``."""
    return classify_user_text(text).intent == TextIntent.RECIPE


if __name__ == "__main__":
    samples = [
        ("привет", TextIntent.GREETING),
        ("спасибо!", TextIntent.THANKS),
        ("как пользоваться ботом?", TextIntent.HELP),
        ("какой лимит на ссылки?", TextIntent.LIMITS),
        ("где книга рецептов", TextIntent.MINI_APP),
        ("какая погода", TextIntent.OFFTOPIC),
        ("ок", TextIntent.UNKNOWN),
        (
            "Борщ\n\nИнгредиенты:\nсвёкла 200 г\nмясо 300 г\n\n"
            "1. Нарезать.\n2. Варить.",
            TextIntent.RECIPE,
        ),
    ]
    for raw, expected in samples:
        got = classify_user_text(raw)
        assert got.intent == expected, f"{raw!r} -> {got.intent}, want {expected}"

    assert not is_recipe_text("привет")
    assert not is_recipe_text("а" * 130), "длина без признаков — не рецепт"
    assert is_recipe_text(
        "Ингредиенты:\n- мука 500 г\n- яйца 2 шт\n\n1. Смешать.\n2. Жарить."
    )
    print("✅ intent_router")
