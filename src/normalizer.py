"""
Модуль нормализации рецептов Remy Bot.

Принимает сырой текст рецепта (результат работы `WebParser`
или любого другого парсера из `src.parser`), прогоняет его через
GitHub Models API (совместимый с OpenAI Chat Completions) и
возвращает строго структурированный `dict` с полями:

* `title`, `description`, `cuisine`, `meal_type`, `dish_type`, `main_ingredient`, `difficulty`,
* `prep_time`, `cook_time`, `total_time`, `servings`,
* `ingredients` (список словарей), `steps` (список словарей),
* `nutrition_per_serving`, `nutrition` (на 100 г),
* `tips`, `storage`, `tags`,
* булевы флаги диет (`is_vegetarian`, `is_vegan`, ...).

После получения JSON от AI выполняется пост-обработка:
пустые/подозрительные значения заменяются безопасными, а
строковые поля (в т.ч. `meal_type`, `dish_type`, `main_ingredient`) приводятся
к латинице в `Localization.normalize_recipe` после `_postprocess`.

Использование:

.. code-block:: python

    normalizer = RecipeNormalizer(github_token=config.github_token)
    data = await normalizer.normalize(raw_text)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

import aiohttp

from .localization import Localization


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Системный промпт
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
You are a professional chef and nutritionist. Extract recipe from the text.

Return ONLY a valid JSON object with this EXACT structure:
{
    "title": "Recipe name in Russian",
    "description": "Brief description in Russian (2-3 sentences)",
    "cuisine": "italian",
    "meal_type": "lunch",
    "dish_type": "main",
    "main_ingredient": "pasta",
    "difficulty": "medium",
    "prep_time": 20,
    "cook_time": 40,
    "total_time": 60,
    "servings": 4,
    "ingredients": [
        {"name": "Мука пшеничная", "amount": 200, "unit": "г", "notes": "просеянная"}
    ],
    "steps": [
        {"step_number": 1, "description": "Разогреть духовку до 180°C"}
    ],
    "nutrition_per_serving": {"calories": 350, "protein": 20, "fat": 15, "carbs": 40},
    "nutrition": {"calories": 150, "protein": 8, "fat": 5, "carbs": 15},
    "tips": ["Совет 1", "Совет 2"],
    "storage": "How to store",
    "tags": ["паста", "италия", "быстро"],
    "is_vegetarian": false,
    "is_vegan": false,
    "is_gluten_free": false,
    "is_lactose_free": false
}

ALLOWED VALUES:
- cuisine: italian, russian, japanese, french, chinese, georgian, korean, indian, thai, mexican, mediterranean, american, european, asian, other
- meal_type: breakfast, lunch, dinner, dessert, snack, salad, soup, baking, drink, other
- dish_type (Latin only): soup, side, salad, appetizer, main, dessert, drink, baking, sauce, preserve
- main_ingredient (Latin only): chicken, beef, pork, fish, seafood, vegetables, mushrooms, eggs, grains, pasta, cheese, fruits, nuts, dough, other
- difficulty: easy, medium, hard

CRITICAL RULES:
1. ALL text fields (title, description, ingredients, steps, tips, storage) MUST be in Russian.
2. meal_type MUST be one of the allowed values.
3. dish_type and main_ingredient MUST be lower-case English keys from the lists above; infer from recipe.
4. difficulty MUST be one of: easy, medium, hard.
5. cuisine MUST be in lower case English from the allowed list.
6. Calculate REALISTIC nutrition based on ingredients and servings.
7. prep_time + cook_time MUST equal total_time.
8. DO NOT use placeholder values like "Untitled" or 0 for required fields.
9. If information is missing, make an EDUCATED GUESS based on similar recipes.
10. Return ONLY valid JSON, no markdown, no additional text.
"""


# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

#: Эндпоинт GitHub Models (совместим с OpenAI Chat Completions).
DEFAULT_API_URL = "https://models.inference.ai.azure.com/chat/completions"

#: Максимальная длина текста, отправляемого в AI (защита от переполнения контекста).
MAX_INPUT_CHARS = 30_000

#: Таймаут HTTP-запроса к AI. Большой, потому что длинные рецепты
#: с расчётом КБЖУ могут генерироваться до минуты.
API_TIMEOUT_SECONDS = 120.0

#: Максимальное число попыток на одну нормализацию. Повтор делается
#: ТОЛЬКО при сбое парсинга JSON, не при HTTP-ошибке/таймауте.
MAX_ATTEMPTS = 2

#: Стоп-слова для эвристического извлечения title из текста.
_TITLE_STOP_WORDS = (
    "ингредиент", "приготовление", "способ", "рецепт", "кухня",
    "описание", "состав",
)

#: Регэксп для снятия markdown-обёртки ```json ... ``` / ``` ... ```.
_MARKDOWN_FENCE_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$",
    re.DOTALL | re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Нормализатор
# --------------------------------------------------------------------------- #

class RecipeNormalizer:
    """Нормализатор рецептов через GitHub Models API.

    Класс обёртывает один-единственный HTTP-эндпоинт и реализует
    устойчивый к ошибкам pipeline «сырой текст → структурированный dict».

    Attributes:
        github_token: PAT с правом вызова GitHub Models.
        model: Имя модели в Models API (по умолчанию ``"gpt-4o-mini"``).
        api_url: Полный URL эндпоинта Chat Completions.
    """

    def __init__(
        self,
        github_token: str,
        model: str = "gpt-4o-mini",
        api_url: str = DEFAULT_API_URL,
    ) -> None:
        """Инициализировать нормализатор.

        Args:
            github_token: Токен для авторизации в GitHub Models.
            model: Имя модели (например, ``"gpt-4o-mini"``).
            api_url: URL эндпоинта Chat Completions. По умолчанию —
                публичный эндпоинт GitHub Models.
        """
        if not github_token:
            raise ValueError("github_token не должен быть пустым")

        self.github_token: str = github_token
        self.model: str = model
        self.api_url: str = api_url

    # ------------------------------------------------------------------ #
    # Основной публичный метод
    # ------------------------------------------------------------------ #

    async def normalize(self, raw_text: str) -> Dict[str, Any]:
        """Превратить сырой текст рецепта в структурированный `dict`.

        Args:
            raw_text: Текст рецепта, обычно полученный от парсера
                (`WebParser.parse`).

        Returns:
            Словарь с полями рецепта, гарантированно содержащий
            нормализованные `meal_type`, `difficulty`, `cuisine`
            (через `Localization.normalize_recipe`) и все обязательные
            поля с безопасными значениями по умолчанию.

        Raises:
            ValueError: если `raw_text` пустой или состоит только из
                пробелов.
            RuntimeError: при ошибке API, таймауте или если AI два раза
                подряд вернул невалидный JSON.
        """
        if not raw_text or not raw_text.strip():
            raise ValueError("Пустой текст рецепта")

        if len(raw_text) > MAX_INPUT_CHARS:
            logger.info(
                "✂️  Текст обрезан до %s символов (лимит контекста)",
                self._format_number(MAX_INPUT_CHARS),
            )
            raw_text = raw_text[:MAX_INPUT_CHARS]

        data: Optional[Dict[str, Any]] = None
        last_parse_error: Optional[Exception] = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            raw_content = await self._call_api(raw_text)

            try:
                data = self._parse_json(raw_content)
                break
            except json.JSONDecodeError as exc:
                last_parse_error = exc
                logger.error(
                    "❌ Ошибка парсинга JSON (попытка %d/%d): %s",
                    attempt, MAX_ATTEMPTS, exc,
                )
                if attempt < MAX_ATTEMPTS:
                    logger.info("🔁 Повторная попытка запроса к AI...")

        if data is None:
            raise RuntimeError(
                f"Не удалось получить валидный JSON от AI: {last_parse_error}"
            )

        data = self._postprocess(data, raw_text)
        data = Localization.normalize_recipe(data)

        logger.info(
            "✅ Рецепт нормализован: «%s», meal_type=%s, %d ингредиентов, %d шагов",
            data.get("title", ""),
            data.get("meal_type", ""),
            len(data.get("ingredients", [])),
            len(data.get("steps", [])),
        )
        return data

    # ------------------------------------------------------------------ #
    # HTTP-слой
    # ------------------------------------------------------------------ #

    async def _call_api(self, user_text: str) -> str:
        """Выполнить POST-запрос к Chat Completions и вернуть `content`.

        Возвращает *строку* — содержимое `choices[0].message.content`,
        которое уже должно быть валидным JSON-объектом (благодаря
        `response_format={"type": "json_object"}`). Снятие markdown-
        обёртки и собственно парсинг делает `_parse_json`.

        Raises:
            RuntimeError: при таймауте, сетевой ошибке или HTTP-статусе
                ≥ 400.
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            "temperature": 0.3,
            "max_tokens": 2500,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)

        logger.info(
            "🤖 Отправка запроса к GitHub Models API (модель: %s)", self.model
        )

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.post(self.api_url, json=payload) as response:
                    body = await response.text(errors="replace")

                    logger.info(
                        "📡 Ответ получен (статус: %d, %d символов)",
                        response.status, len(body),
                    )

                    if response.status >= 400:
                        logger.error(
                            "❌ Ошибка API (статус %d): %s",
                            response.status, self._truncate(body, 500),
                        )
                        raise RuntimeError(
                            f"Ошибка GitHub Models API (статус {response.status})"
                        )

                    return self._extract_content(body)

        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при обращении к AI (%.0f с)", API_TIMEOUT_SECONDS)
            raise RuntimeError("Таймаут при обращении к AI") from None
        except aiohttp.ClientError as exc:
            logger.error("❌ Сетевая ошибка при обращении к AI: %s", exc)
            raise RuntimeError(f"Сетевая ошибка при обращении к AI: {exc}") from exc

    @staticmethod
    def _extract_content(response_body: str) -> str:
        """Вытащить `choices[0].message.content` из ответа API.

        Raises:
            RuntimeError: если ответ не является JSON или в нём нет
                ожидаемой структуры.
        """
        try:
            envelope = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Тело ответа API не является JSON: {exc}") from exc

        try:
            content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Неожиданная структура ответа API: {exc} (тело: {response_body[:300]!r})"
            ) from exc

        if not isinstance(content, str):
            raise RuntimeError(
                f"Поле content имеет тип {type(content).__name__}, ожидалась строка"
            )
        return content

    # ------------------------------------------------------------------ #
    # JSON-парсинг
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_json(raw_content: str) -> Dict[str, Any]:
        """Распарсить JSON из строки, снятой с ответа AI.

        Допускает обёртку ``` ```json ... ``` ```, пустое/отсутствующее
        значение трактуется как ошибка.

        Raises:
            json.JSONDecodeError: если содержимое не является JSON.
        """
        text = raw_content.strip() if raw_content else ""
        if not text:
            raise json.JSONDecodeError("Пустой ответ AI", doc="", pos=0)

        match = _MARKDOWN_FENCE_RE.match(text)
        if match:
            text = match.group("body").strip()

        data = json.loads(text)

        if not isinstance(data, dict):
            raise json.JSONDecodeError(
                f"Ожидался JSON-объект, получено {type(data).__name__}",
                doc=text, pos=0,
            )
        return data

    # ------------------------------------------------------------------ #
    # Пост-обработка
    # ------------------------------------------------------------------ #

    def _postprocess(self, data: Dict[str, Any], raw_text: str) -> Dict[str, Any]:
        """Подправить пустые/подозрительные значения на разумные.

        Мутирует и возвращает тот же `dict` для удобства сцепления
        вызовов.
        """
        # ---- title ----
        title = str(data.get("title") or "").strip()
        if not title or title.lower() in {"untitled", "recipe", "рецепт"}:
            logger.warning("⚠️  AI вернул пустой title, извлекаю из текста...")
            extracted = self._extract_title_from_text(raw_text)
            logger.info("📝 Title извлечён из текста: «%s»", extracted)
            title = extracted
        data["title"] = title

        # ---- description ----
        data["description"] = str(data.get("description") or "").strip()

        # ---- времена ----
        prep_time = self._to_int(data.get("prep_time"), default=0)
        cook_time = self._to_int(data.get("cook_time"), default=0)
        total_time = self._to_int(data.get("total_time"), default=0)
        if total_time <= 0:
            total_time = prep_time + cook_time
        data["prep_time"] = max(prep_time, 0)
        data["cook_time"] = max(cook_time, 0)
        data["total_time"] = max(total_time, 0)

        # ---- порции ----
        servings = self._to_int(data.get("servings"), default=0)
        if servings <= 0:
            servings = 4
        data["servings"] = servings

        # ---- ingredients ----
        ingredients = data.get("ingredients")
        if not isinstance(ingredients, list) or not ingredients:
            logger.warning(
                "⚠️  AI не вернул ingredients, пытаюсь извлечь из текста..."
            )
            ingredients = self._extract_ingredients_from_text(raw_text)
        data["ingredients"] = [self._normalize_ingredient(i) for i in ingredients]

        # ---- steps ----
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            logger.warning("⚠️  AI не вернул steps, пытаюсь извлечь из текста...")
            steps = self._extract_steps_from_text(raw_text)
        data["steps"] = [self._normalize_step(s, i) for i, s in enumerate(steps, 1)]

        # ---- nutrition ----
        data["nutrition_per_serving"] = self._normalize_nutrition(
            data.get("nutrition_per_serving")
        )
        data["nutrition"] = self._normalize_nutrition(data.get("nutrition"))

        # ---- массивы ----
        data["tips"] = self._clean_string_list(data.get("tips"))
        data["tags"] = self._clean_string_list(data.get("tags"))

        # ---- остальное ----
        data["storage"] = str(data.get("storage") or "").strip()
        for flag in ("is_vegetarian", "is_vegan", "is_gluten_free", "is_lactose_free"):
            data[flag] = bool(data.get(flag, False))

        # dish_type / main_ingredient нормализуются один раз в normalize_recipe()
        return data

    # ------------------------------------------------------------------ #
    # Эвристики-фолбэки
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_title_from_text(text: str) -> str:
        """Достать заголовок из первого предложения текста.

        Ищет первую непустую строку длиной 5–100 символов,
        пропуская строки со стоп-словами («ингредиент»,
        «приготовление», «способ», «рецепт», «кухня», ...).
        Если подходящей не нашлось — возвращает первые 100 символов
        текста, приведённые в одну строку.
        """
        for line in text.splitlines():
            stripped = line.strip()
            if not (5 <= len(stripped) <= 100):
                continue
            low = stripped.lower()
            if any(stop in low for stop in _TITLE_STOP_WORDS):
                continue
            return stripped

        fallback = " ".join(text.split())[:100].strip()
        return fallback or "Без названия"

    @staticmethod
    def _extract_ingredients_from_text(text: str) -> List[Dict[str, Any]]:
        """Грубо вытащить список ингредиентов из текста.

        Правило: берём короткие строки, в которых есть цифра (это почти
        наверняка количество). Это последний рубеж — AI должен был
        справиться сам.
        """
        results: List[Dict[str, Any]] = []
        seen: set = set()

        for line in text.splitlines():
            stripped = line.strip(" •-–—\t")
            if not (3 <= len(stripped) <= 200):
                continue
            if not re.search(r"\d", stripped):
                continue
            low = stripped.lower()
            if any(stop in low for stop in _TITLE_STOP_WORDS):
                continue
            if stripped in seen:
                continue
            seen.add(stripped)
            results.append({"name": stripped, "amount": 0, "unit": "", "notes": ""})
            if len(results) >= 30:
                break
        return results

    @staticmethod
    def _extract_steps_from_text(text: str) -> List[Dict[str, Any]]:
        """Грубо вытащить шаги приготовления.

        Делит текст на предложения по точкам и оставляет те,
        что длиннее 15 символов и короче 400. Как и ингредиенты —
        это страховка на случай капризов AI.
        """
        chunks = re.split(r"(?<=[.!?])\s+", text)
        results: List[Dict[str, Any]] = []
        for chunk in chunks:
            s = chunk.strip()
            if not (15 <= len(s) <= 400):
                continue
            low = s.lower()
            if any(stop in low for stop in ("ингредиент", "состав")):
                continue
            results.append({"step_number": len(results) + 1, "description": s})
            if len(results) >= 20:
                break
        return results

    # ------------------------------------------------------------------ #
    # Мелкие утилиты
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_ingredient(raw: Any) -> Dict[str, Any]:
        """Привести один ингредиент к схеме `{name, amount, unit, notes}`."""
        if not isinstance(raw, dict):
            return {"name": str(raw).strip(), "amount": 0, "unit": "", "notes": ""}
        return {
            "name": str(raw.get("name") or "").strip(),
            "amount": RecipeNormalizer._to_number(raw.get("amount"), default=0),
            "unit": str(raw.get("unit") or "").strip(),
            "notes": str(raw.get("notes") or "").strip(),
        }

    @staticmethod
    def _normalize_step(raw: Any, index: int) -> Dict[str, Any]:
        """Привести один шаг к схеме `{step_number, description}`."""
        if isinstance(raw, str):
            return {"step_number": index, "description": raw.strip()}
        if not isinstance(raw, dict):
            return {"step_number": index, "description": str(raw).strip()}
        step_number = RecipeNormalizer._to_int(raw.get("step_number"), default=index)
        description = str(raw.get("description") or "").strip()
        return {"step_number": step_number, "description": description}

    @staticmethod
    def _normalize_nutrition(raw: Any) -> Dict[str, int]:
        """Привести блок КБЖУ к четырём числовым полям."""
        src = raw if isinstance(raw, dict) else {}
        return {
            "calories": RecipeNormalizer._to_int(src.get("calories"), default=0),
            "protein": RecipeNormalizer._to_int(src.get("protein"), default=0),
            "fat": RecipeNormalizer._to_int(src.get("fat"), default=0),
            "carbs": RecipeNormalizer._to_int(src.get("carbs"), default=0),
        }

    @staticmethod
    def _clean_string_list(raw: Any) -> List[str]:
        """Привести значение к списку непустых строк.

        Отбрасывает `None`, пустые строки и элементы, превращающиеся
        в пустоту после ``strip()``. `None`-элементы внутри списка
        не конвертируются в строку ``"None"``.
        """
        if not isinstance(raw, list):
            return []
        result: List[str] = []
        for item in raw:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                result.append(text)
        return result

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        """Мягкое приведение к `int`. При ошибке возвращает `default`."""
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_number(value: Any, default: float = 0) -> float:
        """Мягкое приведение к `float`. При ошибке возвращает `default`."""
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _format_number(value: int) -> str:
        """Отформатировать число с пробелом-разделителем тысяч."""
        return f"{value:,}".replace(",", " ")

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Укоротить строку для логирования."""
        if len(text) <= limit:
            return text
        return text[:limit] + "…"


# --------------------------------------------------------------------------- #
# Тестовый запуск
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv

    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    async def _test() -> None:
        # ---- Тест 2 (без API): пустой текст ----
        temp_normalizer = RecipeNormalizer(github_token="dummy-for-validation")
        try:
            await temp_normalizer.normalize("")
            print("❌ Должна быть ошибка на пустом тексте")
        except ValueError as exc:
            print(f"✅ Пустой текст: {exc}")

        # ---- Тест 1 (с API): реальная нормализация ----
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            print("⚠️  GITHUB_TOKEN не задан — пропускаю тест с реальным API")
            return

        normalizer = RecipeNormalizer(token)

        test_text = """
        Борщ классический
        Ингредиенты: говядина 500г, свёкла 2шт, капуста 300г, картофель 3шт,
        морковь 1шт, лук 1шт, томатная паста 2ст.л.
        Приготовление: Сварить бульон 1 час. Нарезать овощи. Обжарить лук и морковь.
        Добавить свёклу и капусту. Варить 20 минут.
        """

        result = await normalizer.normalize(test_text)
        print(f"✅ Название: {result['title']}")
        print(f"✅ meal_type: {result['meal_type']}")
        print(f"✅ difficulty: {result['difficulty']}")
        print(f"✅ cuisine: {result['cuisine']}")
        print(f"✅ Ингредиентов: {len(result['ingredients'])}")
        print(f"✅ Шагов: {len(result['steps'])}")
        print(f"✅ КБЖУ (на порцию): {result['nutrition_per_serving']}")
        print(f"✅ КБЖУ (на 100 г): {result['nutrition']}")
        print(f"✅ Время: prep={result['prep_time']} + cook={result['cook_time']} = total={result['total_time']}")

    asyncio.run(_test())
