"""
Модуль нормализации рецептов Remy Bot.

Принимает сырой текст рецепта (результат работы `WebParser`
или любого другого парсера из `src.parser`), прогоняет его через
GitHub Models API (совместимый с OpenAI Chat Completions) и
возвращает строго структурированный `dict` с полями:

* `title`, `description`, `cuisine`, `meal_type`, `dish_type`, `main_ingredient`, `difficulty`,
* `prep_time`, `cook_time`, `total_time`, `servings`,
* `ingredients` (список словарей `{name, amount, unit, notes, estimated}`),
  `steps` (список словарей),
* `nutrition_per_serving`, `nutrition` (на 100 г),
* `tips`, `storage`, `tags`,
* булевы флаги диет (`is_vegetarian`, `is_vegan`, ...).

После получения JSON от AI выполняется пост-обработка:
пустые/подозрительные значения заменяются безопасными; при низкой
уверенности AI в блоках ``ingredients`` / ``steps`` (поле ``confidence``)
в начало ``description`` добавляется дисклеймер о точности.
Строковые поля (в т.ч. ``meal_type``, ``dish_type``, ``main_ingredient``)
приводятся к латинице в ``Localization.normalize_recipe`` после ``_postprocess``.

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
from .recipe_metrics import (
    normalize_recipe_times,
    refine_servings,
    strip_redundant_nutrition_note,
)


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Системный промпт
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """\
Вы — профессиональный шеф-повар и дотошный кулинарный редактор. Ваша задача — \
трансформировать неструктурированный текст (субтитры, транскрипты, хаотичные описания) \
в идеальную технологическую карту рецепта.

СТРОГИЕ ПРАВИЛА:
1. КУЛИНАРНАЯ ТОЧНОСТЬ И ЛОГИКА: Внимательно анализируйте контекст. Если ингредиент \
используется необычным способом (например, запекается целая головка чеснока для соуса, \
сухари настаиваются 24 часа перед фильтрацией), вы ОБЯЗАНЫ полностью сохранить эту \
кулинарную технологию в шагах приготовления. Никогда не заменяйте уникальные авторские \
фишки на стандартные бытовые шаблоны (например, запекание на обжарку на сковороде).
2. ЗАПРЕТ НА ДОДУМЫВАНИЕ ИНГРЕДИЕНТОВ: Записывайте строго те ингредиенты, которые прямо \
упомянуты в источнике. Если точный вес не указан, пишите «по вкусу», «на глаз» или \
сохраняйте штуки/ложки так, как сказал автор. Категорически запрещено выдумывать точные \
граммовки (например, «350 г филе 70/30»), если их не было в исходном тексте.
3. ПРАВИЛО КБЖУ: \
- Если в источнике указаны точные граммовки ВСЕХ основных калорийных ингредиентов — рассчитайте \
КБЖУ точно: nutrition_estimated=false, nutrition_calculable=true, заполните nutrition_* цифрами. \
- Если граммовки частичные, «на глаз» или неполные — дайте обоснованную ОЦЕНКУ КБЖУ: \
nutrition_estimated=true, nutrition_calculable=false, заполните nutrition_* оценочными значениями. \
- Если оценить нельзя (нет состава) — нули, nutrition_estimated=false, nutrition_calculable=false.
- nutrition_note заполняйте ТОЛЬКО если КБЖУ посчитать невозможно вовсе; для оценочных \
цифр nutrition_note оставляйте пустым (достаточно nutrition_estimated=true).
4. ВРЕМЯ (все поля — в МИНУТАХ):
- prep_time: активная подготовка (нарезка, замес, смешивание, формовка) — БЕЗ пассивного \
ожидания (расстойка, маринад, охлаждение в холодильнике).
- cook_time: активная готовка с контролем — у плиты, в духовке, на пару (варка, жарка, выпечка).
- total_time: полное календарное время от начала до подачи, ВКЛЮЧАЯ все пассивные паузы \
(расстойка 72 ч = 4320 мин, маринование, охлаждение) плюс активные этапы.
- total_time >= prep_time + cook_time. Пример хлеба: prep 40, cook 45, total 4365 (72 ч расстой + 85 мин).
5. ПОРЦИИ (servings):
- Если в источнике указано число порций — используйте его.
- Иначе оцените по суммарному весу ингредиентов и типу блюда (ориентир USDA RACC: \
суп ~245 г/порция, основное ~250 г, гарнир/салат ~120–140 г, закуска ~85 г, \
десерт/выпечка ~75–120 г, напиток ~250 мл). Округляйте до разумного целого (1–12).
6. ФИЛЬТРАЦИЯ РЕЧЕВЫХ АРТЕФАКТОВ: Игнорируйте звуки-паразиты, шутки автора; исправляйте \
ошибки распознавания аудио (например, «столёная соль» → «соль»).
7. СЕКРЕТЫ ШЕФА: Обязательно выносите в шаги важные технологические предупреждения автора \
(«мешать только деревянной лопаткой», «насухо вытереть ягоды», «срезать специи с корочки»).

ФОРМАТ ВЫВОДА:
Пишите коротко, ёмко, без приветствий. Верните ТОЛЬКО валидный JSON (без markdown):

{
    "title": "Название на русском",
    "description": "Краткое описание (2–3 предложения)",
    "cuisine": "russian",
    "meal_type": "lunch",
    "dish_type": "main",
    "main_ingredient": "beef",
    "difficulty": "medium",
    "prep_time": 20,
    "cook_time": 40,
    "total_time": 60,
    "servings": 4,
    "ingredients": [
        {"name": "Соль", "amount": 0, "unit": "", "notes": "по вкусу", "estimated": false}
    ],
    "steps": [
        {"step_number": 1, "description": "Конкретное действие из источника"}
    ],
    "nutrition_per_serving": {"calories": 0, "protein": 0, "fat": 0, "carbs": 0},
    "nutrition": {"calories": 0, "protein": 0, "fat": 0, "carbs": 0},
    "nutrition_calculable": true,
    "nutrition_estimated": false,
    "nutrition_note": "",
    "tips": [],
    "storage": "",
    "tags": [],
    "image_url": "",
    "confidence": {
        "title": "high",
        "description": "high",
        "ingredients": "high",
        "steps": "high",
        "times": "medium",
        "nutrition": "medium"
    },
    "is_vegetarian": false,
    "is_vegan": false,
    "is_gluten_free": false,
    "is_lactose_free": false
}

ДОПОЛНИТЕЛЬНО:
- estimated=true ТОЛЬКО если вы сами оценили количество при явной нехватке данных; \
никогда не ставьте estimated=true для «по вкусу» / «на глаз» из источника.
- Если шагов нет в источнике — один шаг: «Пошаговая инструкция отсутствует в источнике. \
Рекомендую посмотреть видео.»
- Не выдумывайте ингредиенты, шаги, время. При неопределённости — confidence low/medium.
- meal_type, dish_type, main_ingredient, cuisine, difficulty — только из списков ниже.
- image_url: копировать только из строки [SERVER image_url — copy verbatim...], иначе \"\".
- Каждое значение confidence: high, medium или low.

ALLOWED VALUES:
- cuisine: italian, russian, japanese, french, chinese, georgian, korean, indian, thai, \
mexican, mediterranean, american, european, asian, other
- meal_type: breakfast, lunch, dinner, dessert, snack, salad, soup, baking, drink, other
- dish_type: soup, side, salad, appetizer, main, dessert, drink, baking, sauce, preserve
- main_ingredient: chicken, beef, pork, fish, seafood, vegetables, mushrooms, eggs, \
grains, pasta, cheese, fruits, nuts, dough, other
- difficulty: easy, medium, hard
"""


IMAGE_VISION_SYSTEM_PROMPT = """\
Вы — профессиональный шеф-повар и дотошный кулинарный редактор. Определите, содержит ли \
изображение кулинарный рецепт (ингредиенты, шаги). Соблюдайте те же СТРОГИЕ ПРАВИЛА, что \
в текстовом режиме: не выдумывайте граммовки и КБЖУ без оснований; сохраняйте уникальную \
технологию; выносите предупреждения автора в шаги.

Если рецепт есть — верните JSON рецепта (поля как в SYSTEM_PROMPT, включая nutrition_note, \
nutrition_calculable, confidence) и "is_recipe": true.
Если нет — только {"is_recipe": false, "explanation": "пояснение по-русски"}.
Только валидный JSON, без markdown. Все текстовые поля рецепта на русском."""


#: Дисклеймер при низкой уверенности в ингредиентах/шагах (источник — видео и т. п.).
_LOW_CONFIDENCE_VIDEO_DISCLAIMER = (
    "⚠️ Низкая точность: данные собраны из видео, возможны ошибки"
)

#: Единственный шаг, если в источнике нет пошаговой инструкции (согласовано с SYSTEM_PROMPT).
_STEPS_MISSING_SOURCE_MESSAGE = (
    "Пошаговая инструкция отсутствует в источнике. Рекомендую посмотреть видео."
)

#: Сообщение, когда КБЖУ нельзя посчитать без точных граммовок.
NUTRITION_UNAVAILABLE_MSG = "Невозможно рассчитать без точных граммовок"

#: Модель по умолчанию (GitHub Models / Azure inference).
DEFAULT_MODEL = "gpt-5-mini"

#: Лимит completion-токенов для reasoning-моделей (включая внутренние рассуждения).
REASONING_COMPLETION_TOKENS = 16_384

#: Глаголы действия в типичных шагах рецепта (рус.).
_STEPS_ACTION_VERBS_RE = re.compile(
    r"(?:^|\s)(?:нарез|смеш|вари|жар|запек|добав|полож|снят|подава|разогре|взбив|полей|обжар|"
    r"туши|отвар|остуд|охлад|порез|очист|натер|вылож|смаза|посып|соли|перч|перемеш|залей|вскипяти|"
    r"достань|вынь|уклад|формир|готов|убери|мешай|отправ|вымеш|замес|сформ|укрась|украс|раздел|"
    r"разлей|слей|слить|свари|пожар|отдел|отдели|взбей|залив|заполн|выли|перелож|сбрызг)\w*",
    re.IGNORECASE,
)

#: Количество + единица — признак списка ингредиентов внутри «шага».
_STEPS_QUANTITY_UNITS_RE = re.compile(
    r"\d+\s*(?:г|кг|мл|л|шт|ст\.?\s*л|ч\.?\s*л)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

#: Эндпоинт GitHub Models (совместим с OpenAI Chat Completions).
DEFAULT_API_URL = "https://models.inference.ai.azure.com/chat/completions"

#: Максимальная длина текста, отправляемого в AI (защита от переполнения контекста).
MAX_INPUT_CHARS = 30_000

#: Максимальный размер изображения (байты), отправляемого в vision API.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: Таймаут HTTP-запроса к AI. Большой, потому что длинные рецепты
#: с расчётом КБЖУ могут генерироваться до минуты.
API_TIMEOUT_SECONDS = 120.0

#: Максимальное число попыток на одну нормализацию. Повтор делается
#: ТОЛЬКО при сбое парсинга JSON, не при HTTP-ошибке/таймауте.
MAX_ATTEMPTS = 2

#: Повторы при временной перегрузке провайдера (429/503).
HTTP_RETRY_STATUSES = frozenset({429, 503})
HTTP_RETRY_ATTEMPTS = 2
HTTP_RETRY_BASE_SECONDS = 1.5

#: Запасные модели HF Router, если основная перегружена (429).
HF_FALLBACK_MODELS = (
    "openai/gpt-oss-120b:fireworks-ai",
    "moonshotai/Kimi-K2-Instruct:novita",
    "deepseek-ai/DeepSeek-V3-0324:novita",
)

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

#: «По вкусу» / «на глаз» — не выдумывать граммы в постобработке.
_VAGUE_AMOUNT_NOTES_RE = re.compile(
    r"по\s+вкусу|на\s+глаз",
    re.IGNORECASE,
)


def _model_basename(model: str) -> str:
    return (model or "").strip().lower().split("/")[-1]


def _is_reasoning_model(model: str) -> bool:
    base = _model_basename(model)
    return base.startswith(("gpt-5", "o1", "o3", "o4"))


# --------------------------------------------------------------------------- #
# Нормализатор
# --------------------------------------------------------------------------- #

class RecipeNormalizer:
    """Нормализатор рецептов через GitHub Models API.

    Класс обёртывает один-единственный HTTP-эндпоинт и реализует
    устойчивый к ошибкам pipeline «сырой текст → структурированный dict».

    Attributes:
        github_token: PAT с правом вызова GitHub Models.
        model: Имя модели в Models API (по умолчанию ``openai/gpt-5-mini``).
        api_url: Полный URL эндпоинта Chat Completions.
        reasoning_effort: Усилие рассуждения для GPT-5/o-серии (minimal…high).
    """

    def __init__(
        self,
        github_token: str,
        model: str = DEFAULT_MODEL,
        api_url: str = DEFAULT_API_URL,
        reasoning_effort: str = "medium",
    ) -> None:
        """Инициализировать нормализатор.

        Args:
            github_token: Токен для авторизации в GitHub Models.
            model: Имя модели (например, ``openai/gpt-5-mini``).
            api_url: URL эндпоинта Chat Completions. По умолчанию —
                публичный эндпоинт GitHub Models.
            reasoning_effort: Параметр reasoning_effort для reasoning-моделей.
        """
        if not github_token:
            raise ValueError("github_token не должен быть пустым")

        self.github_token: str = github_token
        self.model: str = model or DEFAULT_MODEL
        self.api_url: str = api_url
        self.reasoning_effort: str = (reasoning_effort or "medium").strip().lower()

    # ------------------------------------------------------------------ #
    # Основной публичный метод
    # ------------------------------------------------------------------ #

    async def normalize(self, raw_text: str, *, image_url: Optional[str] = None) -> Dict[str, Any]:
        """Превратить сырой текст рецепта в структурированный `dict`.

        Args:
            raw_text: Текст рецепта, обычно полученный от парсера
                (`WebParser.parse`).
            image_url: Необязательный URL или локальный путь к изображению — попадает в JSON.

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
            raw_content = await self._call_api(raw_text, image_url=image_url)

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
        if image_url and str(image_url).strip():
            data["image_url"] = str(image_url).strip()

        logger.info(
            "✅ Рецепт нормализован: «%s», meal_type=%s, %d ингредиентов, %d шагов",
            data.get("title", ""),
            data.get("meal_type", ""),
            len(data.get("ingredients", [])),
            len(data.get("steps", [])),
        )
        return data

    async def analyze_image(
        self,
        image_base64: str,
        *,
        mime_type: str = "image/jpeg",
    ) -> Dict[str, Any]:
        """Проанализировать изображение через vision API.

        Returns:
            Либо полный словарь рецепта (как у ``normalize``), без ключа ``is_recipe``,
            либо заглушку ``{"is_recipe": false, "reason": str}``,
            либо ``{"is_recipe": false, "reason": str, "error": True}`` при сбое API/разбора.

        Raises:
            ValueError: пустой ``image_base64``.
        """
        b64 = (image_base64 or "").strip()
        if not b64:
            raise ValueError("Пустое изображение")

        raw_placeholder = "[изображение]"
        models_try: List[str] = []
        for cand in (self.model, "gpt-5-chat", "gpt-4o"):
            if cand and cand not in models_try:
                models_try.append(cand)

        last_http_error: Optional[RuntimeError] = None
        raw_content: Optional[str] = None
        used_model: Optional[str] = None

        for model in models_try:
            try:
                raw_content = await self._call_api_vision(b64, mime_type, model=model)
                used_model = model
                break
            except RuntimeError as exc:
                last_http_error = exc
                logger.warning(
                    "⚠️ Vision-модель %s: %s — пробую следующую при наличии",
                    model,
                    exc,
                )
                continue

        if raw_content is None:
            logger.error("❌ Vision: все модели вернули ошибку: %s", last_http_error)
            return {
                "is_recipe": False,
                "reason": str(last_http_error) if last_http_error else "api",
                "error": True,
            }

        try:
            data = self._parse_json(raw_content)
        except json.JSONDecodeError as exc:
            logger.error("❌ Vision: невалидный JSON от AI: %s", exc)
            return {"is_recipe": False, "reason": "invalid_json", "error": True}

        if data.get("is_recipe") is False:
            expl = (
                data.get("explanation")
                or data.get("reason")
                or data.get("message")
                or ""
            )
            return {
                "is_recipe": False,
                "reason": str(expl).strip(),
            }

        for key in ("is_recipe", "explanation", "message"):
            data.pop(key, None)

        data = self._postprocess(data, raw_placeholder)
        data = Localization.normalize_recipe(data)

        logger.info(
            "✅ Рецепт из изображения нормализован («%s», модель %s)",
            data.get("title", ""),
            used_model or self.model,
        )
        return data

    # ------------------------------------------------------------------ #
    # HTTP-слой
    # ------------------------------------------------------------------ #

    def _apply_completion_limits(self, payload: Dict[str, Any], *, model: Optional[str] = None) -> None:
        """Настроить лимиты токенов под reasoning- и обычные модели."""
        m = model or self.model
        if _is_reasoning_model(m):
            payload["max_completion_tokens"] = REASONING_COMPLETION_TOKENS
            payload["reasoning_effort"] = self.reasoning_effort
            payload.pop("max_tokens", None)
            payload.pop("temperature", None)
        else:
            payload.setdefault("temperature", 0.1)
            payload["max_tokens"] = 8000
            payload.pop("max_completion_tokens", None)
            payload.pop("reasoning_effort", None)

    def _build_chat_payload(
        self,
        *,
        system: str,
        user_content: Any,
        model: Optional[str] = None,
        json_mode: bool = True,
    ) -> Dict[str, Any]:
        m = model or self.model
        payload: Dict[str, Any] = {
            "model": m,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        }
        self._apply_completion_limits(payload, model=m)
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _call_api(self, user_text: str, image_url: Optional[str] = None) -> str:
        """Выполнить POST-запрос к Chat Completions и вернуть `content`.

        Возвращает *строку* — содержимое `choices[0].message.content`,
        которое уже должно быть валидным JSON-объектом (благодаря
        `response_format={"type": "json_object"}`). Снятие markdown-
        обёртки и собственно парсинг делает `_parse_json`.

        Raises:
            RuntimeError: при таймауте, сетевой ошибке или HTTP-статусе
                ≥ 400.
        """
        user_content = user_text
        if image_url and str(image_url).strip():
            user_content = (
                f"{user_text}\n\n[SERVER image_url — copy verbatim to JSON field \"image_url\"]: "
                f"{json.dumps(str(image_url).strip(), ensure_ascii=True)}"
            )
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
        models = self._candidate_models()

        last_status = 0
        last_body = ""
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                for model in models:
                    payload = self._build_chat_payload(
                        system=SYSTEM_PROMPT,
                        user_content=user_content,
                        model=model,
                    )
                    logger.info("🤖 Отправка запроса к LLM API (модель: %s)", model)
                    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
                        async with session.post(self.api_url, json=payload) as response:
                            body = await response.text(errors="replace")
                            last_status = response.status
                            last_body = body

                            logger.info(
                                "📡 Ответ получен (статус: %d, %d символов)",
                                response.status,
                                len(body),
                            )

                            if response.status < 400:
                                return self._extract_content(body)

                            if (
                                response.status in HTTP_RETRY_STATUSES
                                and attempt < HTTP_RETRY_ATTEMPTS
                            ):
                                wait = HTTP_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                                logger.warning(
                                    "⏳ LLM перегружен (HTTP %d, %s), повтор через "
                                    "%.1f с (%d/%d)",
                                    response.status,
                                    model,
                                    wait,
                                    attempt,
                                    HTTP_RETRY_ATTEMPTS,
                                )
                                await asyncio.sleep(wait)
                                continue

                            if response.status in HTTP_RETRY_STATUSES:
                                logger.warning(
                                    "⚠️ Модель %s недоступна (HTTP %d), пробую другую",
                                    model,
                                    response.status,
                                )
                                break

                            logger.error(
                                "❌ Ошибка API (статус %d): %s",
                                response.status,
                                self._truncate(body, 500),
                            )
                            raise RuntimeError(
                                f"Ошибка LLM API (статус {response.status})"
                            )

        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при обращении к AI (%.0f с)", API_TIMEOUT_SECONDS)
            raise RuntimeError("Таймаут при обращении к AI") from None
        except aiohttp.ClientError as exc:
            logger.error("❌ Сетевая ошибка при обращении к AI: %s", exc)
            raise RuntimeError(f"Сетевая ошибка при обращении к AI: {exc}") from exc

        logger.error(
            "❌ Ошибка API после повторов (статус %d): %s",
            last_status,
            self._truncate(last_body, 500),
        )
        raise RuntimeError(f"Ошибка LLM API (статус {last_status})")

    def _candidate_models(self) -> list[str]:
        """Основная модель + запасные (для HF Router при 429)."""
        primary = (self.model or "").strip()
        models: list[str] = []
        if primary:
            models.append(primary)
        if "huggingface.co" in (self.api_url or ""):
            for m in HF_FALLBACK_MODELS:
                if m not in models:
                    models.append(m)
        return models or [DEFAULT_MODEL]

    async def _call_api_vision(
        self,
        image_base64: str,
        mime_type: str,
        *,
        model: str,
    ) -> str:
        """Vision-запрос (image URL data-uri + текст). ``max_tokens`` по ТЗ — 2000."""
        data_url = f"data:{mime_type};base64,{image_base64}"
        payload = self._build_chat_payload(
            system=IMAGE_VISION_SYSTEM_PROMPT,
            user_content=[
                {
                    "type": "text",
                    "text": "Проанализируй это изображение на наличие рецепта.",
                },
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
            model=model,
        )
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)

        logger.info("🤖 Отправка vision-запроса к LLM API (модель: %s)", model)

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.post(self.api_url, json=payload) as response:
                    body = await response.text(errors="replace")
                    logger.info(
                        "📡 Vision-ответ (статус: %d, %d символов)",
                        response.status,
                        len(body),
                    )
                    if response.status >= 400:
                        logger.error(
                            "❌ Vision API (статус %d): %s",
                            response.status,
                            self._truncate(body, 500),
                        )
                        raise RuntimeError(
                            f"Ошибка GitHub Models vision API (статус {response.status})"
                        )
                    return self._extract_content(body)
        except asyncio.TimeoutError:
            logger.error("❌ Таймаут vision-запроса к AI (%.0f с)", API_TIMEOUT_SECONDS)
            raise RuntimeError("Таймаут при обращении к AI (vision)") from None
        except aiohttp.ClientError as exc:
            logger.error("❌ Сетевая ошибка vision: %s", exc)
            raise RuntimeError(f"Сетевая ошибка при vision: {exc}") from exc

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
        prep_time, cook_time, total_time = normalize_recipe_times(
            self._to_int(data.get("prep_time"), default=0),
            self._to_int(data.get("cook_time"), default=0),
            self._to_int(data.get("total_time"), default=0),
        )
        data["prep_time"] = prep_time
        data["cook_time"] = cook_time
        data["total_time"] = total_time

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
        self._replace_steps_if_not_actionable(data)

        # ---- порции (после нормализации ингредиентов) ----
        data["servings"] = refine_servings(
            data.get("ingredients") or [],
            dish_type=str(data.get("dish_type") or "main"),
            meal_type=str(data.get("meal_type") or "other"),
            raw_text=raw_text,
            ai_servings=self._to_int(data.get("servings"), default=0),
        )

        # ---- nutrition ----
        data["nutrition_per_serving"] = self._normalize_nutrition(
            data.get("nutrition_per_serving")
        )
        data["nutrition"] = self._normalize_nutrition(data.get("nutrition"))
        data["nutrition_note"] = strip_redundant_nutrition_note(
            str(data.get("nutrition_note") or "").strip(),
            estimated=bool(data.get("nutrition_estimated")),
        )
        data["nutrition_estimated"] = bool(data.get("nutrition_estimated", False))
        self._apply_nutrition_policy(data)
        data.pop("nutrition_calculable", None)

        # ---- массивы ----
        data["tips"] = self._clean_string_list(data.get("tips"))
        data["tags"] = self._clean_string_list(data.get("tags"))

        # ---- остальное ----
        data["storage"] = str(data.get("storage") or "").strip()
        img = str(data.get("image_url") or "").strip()
        if not img:
            img = str(data.get("image_path") or "").strip()
        data["image_url"] = img
        data.pop("image_path", None)
        for flag in ("is_vegetarian", "is_vegan", "is_gluten_free", "is_lactose_free"):
            data[flag] = bool(data.get(flag, False))

        # dish_type / main_ingredient нормализуются один раз в normalize_recipe()
        self._apply_low_confidence_video_disclaimer(data)
        return data

    @staticmethod
    def _compact_text_for_compare(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").lower().strip())

    @staticmethod
    def _steps_look_like_ingredients_or_description(
        steps: List[Dict[str, Any]],
        description: str,
    ) -> bool:
        """Эвристика: шаги — список продуктов или копия описания, а не действия."""
        parts = [
            str(s.get("description") or "").strip()
            for s in steps
            if isinstance(s, dict)
        ]
        combined = " ".join(parts).strip()
        if len(combined) < 40:
            return False
        if _STEPS_MISSING_SOURCE_MESSAGE.lower() in combined.lower():
            return False

        desc_c = RecipeNormalizer._compact_text_for_compare(description)
        comb_c = RecipeNormalizer._compact_text_for_compare(combined)
        if desc_c and comb_c:
            if comb_c in desc_c or desc_c in comb_c:
                return True
            dw = set(desc_c.split())
            cw = set(comb_c.split())
            if len(cw) >= 10 and len(dw & cw) / max(len(cw), 1) >= 0.55:
                return True

        unit_hits = len(_STEPS_QUANTITY_UNITS_RE.findall(combined))
        verb_hits = len(_STEPS_ACTION_VERBS_RE.findall(combined))
        if unit_hits >= 4 and verb_hits <= 1:
            return True
        if unit_hits >= 3 and verb_hits == 0:
            return True
        return False

    def _replace_steps_if_not_actionable(self, data: Dict[str, Any]) -> None:
        """Заменить подозрительные шаги на дисклеймер (видео / нет чёткой инструкции)."""
        steps = data.get("steps")
        if not isinstance(steps, list) or not steps:
            return
        desc = str(data.get("description") or "")
        if not self._steps_look_like_ingredients_or_description(steps, desc):
            return
        logger.warning(
            "⚠️  Шаги похожи на ингредиенты или дублируют описание — подставлен дисклеймер"
        )
        data["steps"] = [
            {"step_number": 1, "description": _STEPS_MISSING_SOURCE_MESSAGE},
        ]
        conf = data.get("confidence")
        if not isinstance(conf, dict):
            data["confidence"] = {}
            conf = data["confidence"]
        conf["steps"] = "low"

    @staticmethod
    def _confidence_is_low(value: Any) -> bool:
        return str(value or "").strip().lower() == "low"

    def _apply_low_confidence_video_disclaimer(self, data: Dict[str, Any]) -> None:
        """Если AI пометил ингредиенты или шаги как low — добавить дисклеймер в описание."""
        conf = data.get("confidence")
        if not isinstance(conf, dict):
            return
        if not (
            self._confidence_is_low(conf.get("ingredients"))
            or self._confidence_is_low(conf.get("steps"))
        ):
            return
        desc = str(data.get("description") or "").strip()
        prefix = _LOW_CONFIDENCE_VIDEO_DISCLAIMER
        if desc.startswith(prefix):
            return
        data["description"] = f"{prefix}\n\n{desc}" if desc else prefix

    def _apply_nutrition_policy(self, data: Dict[str, Any]) -> None:
        """Сохранить оценочное/точное КБЖУ; не обнулять обоснованные оценки модели."""
        conf = data.get("confidence")
        if not isinstance(conf, dict):
            conf = {}
            data["confidence"] = conf

        nps = data.get("nutrition_per_serving") or {}
        has_numbers = isinstance(nps, dict) and any(
            self._to_int(nps.get(k), default=0) > 0 for k in ("calories", "protein", "fat", "carbs")
        )

        calculable = data.get("nutrition_calculable")
        estimated = bool(data.get("nutrition_estimated"))
        note = str(data.get("nutrition_note") or "").strip()

        if self._confidence_is_low(conf.get("nutrition")):
            if has_numbers:
                estimated = True
                calculable = False
            else:
                data["nutrition_per_serving"] = {
                    "calories": 0, "protein": 0, "fat": 0, "carbs": 0,
                }
                data["nutrition"] = {"calories": 0, "protein": 0, "fat": 0, "carbs": 0}
                data["nutrition_estimated"] = False
                data["nutrition_note"] = ""
                conf["nutrition"] = "low"
                return

        if calculable is False or estimated:
            data["nutrition_estimated"] = True
            data["nutrition_calculable"] = False
            data["nutrition_note"] = strip_redundant_nutrition_note(note, estimated=True)
        elif calculable is True and has_numbers:
            data["nutrition_estimated"] = False
            data["nutrition_note"] = ""
        elif has_numbers:
            data["nutrition_estimated"] = estimated
            data["nutrition_note"] = strip_redundant_nutrition_note(note, estimated=estimated)
        else:
            data["nutrition_estimated"] = False
            data["nutrition_note"] = ""

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
        """Привести один ингредиент к схеме `{name, amount, unit, notes, estimated}`."""
        if not isinstance(raw, dict):
            return {
                "name": str(raw).strip(),
                "amount": 0,
                "unit": "",
                "notes": "",
                "estimated": False,
            }
        name = str(raw.get("name") or "").strip()
        amount = RecipeNormalizer._to_number(raw.get("amount"), default=0)
        unit = str(raw.get("unit") or "").strip()
        notes = str(raw.get("notes") or "").strip()
        estimated = bool(raw.get("estimated", False))
        vague = bool(_VAGUE_AMOUNT_NOTES_RE.search(notes))

        if vague or (estimated and amount <= 0):
            estimated = False
            amount = max(amount, 0)
            if vague and not notes:
                notes = "по вкусу"

        if estimated and amount > 0:
            low_unit = unit.lower()
            if low_unit in {"гр"}:
                unit = "г"
            elif low_unit in {"штука", "штуки", "штук"}:
                unit = "шт"
            if unit.lower() not in {"г", "гр", "мл", "шт", "штука", "штуки", "штук", "ст.л.", "ч.л.", "ст. л.", "ч. л."}:
                unit = "г"
            if "*" not in notes:
                notes = f"{notes} *".strip() if notes else "*"
            logger.info(
                "Ингредиент %s: количество оценено ИИ как %s %s",
                name or "без названия",
                amount,
                unit,
            )

        return {
            "name": name,
            "amount": amount,
            "unit": unit,
            "notes": notes,
            "estimated": estimated,
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
    from unittest.mock import AsyncMock, patch

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

        # ---- analyze_image (мок vision) ----
        with patch.object(RecipeNormalizer, "_call_api_vision", new_callable=AsyncMock) as mock_v:
            mock_v.return_value = '{"is_recipe": false, "explanation": "на фото нет рецепта"}'
            r_img = await temp_normalizer.analyze_image("dGVzdA==")
            assert r_img.get("is_recipe") is False
            assert r_img.get("error") is not True

            mock_v.return_value = json.dumps({
                "is_recipe": True,
                "title": "Борщ тестовый",
                "description": "Описание",
                "cuisine": "russian",
                "meal_type": "lunch",
                "dish_type": "soup",
                "main_ingredient": "beef",
                "difficulty": "medium",
                "prep_time": 10,
                "cook_time": 50,
                "total_time": 60,
                "servings": 4,
                "ingredients": [{"name": "Вода", "amount": 1, "unit": "л", "notes": ""}],
                "steps": [{"step_number": 1, "description": "Варить."}],
                "nutrition_per_serving": {"calories": 0, "protein": 0, "fat": 0, "carbs": 0},
                "nutrition": {"calories": 0, "protein": 0, "fat": 0, "carbs": 0},
                "tips": [],
                "storage": "",
                "tags": [],
                "confidence": {
                    "title": "high",
                    "description": "high",
                    "ingredients": "high",
                    "steps": "high",
                    "times": "medium",
                    "nutrition": "low",
                },
                "is_vegetarian": False,
                "is_vegan": False,
                "is_gluten_free": False,
                "is_lactose_free": False,
            })
            r_ok = await temp_normalizer.analyze_image("dGVzdA==", mime_type="image/png")
            assert r_ok.get("is_recipe") is not False
            assert (r_ok.get("title") or "").startswith("Борщ")
            assert r_ok.get("ingredients")

        print("✅ analyze_image: мок vision API")

        assert _is_reasoning_model("gpt-5-mini") is True
        assert _is_reasoning_model("gpt-4o-mini") is False
        payload: Dict[str, Any] = {"model": "gpt-5-mini", "messages": []}
        temp_normalizer._apply_completion_limits(payload)
        assert "max_completion_tokens" in payload
        assert payload.get("reasoning_effort") == "medium"
        assert "max_tokens" not in payload
        print("✅ reasoning payload (gpt-5-mini)")

        # ---- estimated ingredients (мок text API, без сети) ----
        with patch.object(RecipeNormalizer, "_call_api", new_callable=AsyncMock) as mock_text:
            mock_text.return_value = json.dumps({
                "title": "Паста с томатами",
                "description": "Тест",
                "cuisine": "italian",
                "meal_type": "lunch",
                "dish_type": "main",
                "main_ingredient": "pasta",
                "difficulty": "easy",
                "prep_time": 5,
                "cook_time": 10,
                "total_time": 15,
                "servings": 2,
                "ingredients": [
                    {
                        "name": "Соль",
                        "amount": 0,
                        "unit": "",
                        "notes": "по вкусу",
                        "estimated": True,
                    }
                ],
                "steps": [{"step_number": 1, "description": "Сварить пасту."}],
                "nutrition_per_serving": {"calories": 320, "protein": 10, "fat": 8, "carbs": 45},
                "nutrition": {"calories": 0, "protein": 0, "fat": 0, "carbs": 0},
                "nutrition_calculable": False,
                "nutrition_estimated": True,
                "nutrition_note": "",
                "tips": [],
                "storage": "",
                "tags": [],
                "confidence": {
                    "title": "high",
                    "description": "high",
                    "ingredients": "medium",
                    "steps": "high",
                    "times": "medium",
                    "nutrition": "medium",
                },
                "is_vegetarian": True,
                "is_vegan": True,
                "is_gluten_free": False,
                "is_lactose_free": True,
            })
            r_est = await temp_normalizer.normalize("Паста. Соль по вкусу.")
        est_ing = r_est["ingredients"][0]
        assert est_ing["estimated"] is False
        assert est_ing["amount"] == 0
        assert "по вкусу" in est_ing["notes"]
        assert "*" not in est_ing["notes"]
        assert r_est.get("nutrition_estimated") is True
        assert r_est["nutrition_per_serving"].get("calories") == 320
        print("✅ «по вкусу» без выдуманных граммов + оценочное КБЖУ")

        with patch.object(RecipeNormalizer, "_call_api", new_callable=AsyncMock) as mock_text2:
            mock_text2.return_value = json.dumps({
                "title": "Стейк",
                "description": "Тест",
                "cuisine": "american",
                "meal_type": "dinner",
                "dish_type": "main",
                "main_ingredient": "beef",
                "difficulty": "medium",
                "prep_time": 5,
                "cook_time": 10,
                "total_time": 15,
                "servings": 2,
                "ingredients": [
                    {
                        "name": "Говядина",
                        "amount": 350,
                        "unit": "г",
                        "notes": "",
                        "estimated": True,
                    }
                ],
                "steps": [{"step_number": 1, "description": "Жарить."}],
                "nutrition_per_serving": {"calories": 400, "protein": 30, "fat": 20, "carbs": 0},
                "nutrition": {"calories": 0, "protein": 0, "fat": 0, "carbs": 0},
                "nutrition_calculable": True,
                "nutrition_estimated": False,
                "nutrition_note": "",
                "tips": [],
                "storage": "",
                "tags": [],
                "confidence": {
                    "title": "high",
                    "description": "high",
                    "ingredients": "high",
                    "steps": "high",
                    "times": "medium",
                    "nutrition": "high",
                },
                "is_vegetarian": False,
                "is_vegan": False,
                "is_gluten_free": True,
                "is_lactose_free": True,
            })
            r_explicit = await temp_normalizer.normalize("Говядина 350 г. Жарить.")
        exp_ing = r_explicit["ingredients"][0]
        assert exp_ing["estimated"] is True
        assert exp_ing["amount"] == 350
        assert "*" in exp_ing["notes"]
        assert r_explicit.get("nutrition_estimated") is False
        print("✅ явная оценка граммов сохраняется")

        # ---- Тест 1 (с API): реальная нормализация ----
        token = os.getenv("GITHUB_TOKEN")
        if not token:
            print("⚠️  GITHUB_TOKEN не задан — пропускаю тест с реальным API")
            return

        normalizer = RecipeNormalizer(
            token,
            model=os.getenv("GITHUB_MODEL", DEFAULT_MODEL),
            reasoning_effort=os.getenv("GITHUB_REASONING_EFFORT", "medium"),
        )

        test_text = """
        Борщ классический
        Ингредиенты: говядина 500г, свёкла 2шт, капуста 300г, картофель 3шт,
        морковь 1шт, лук 1шт, томатная паста 2ст.л.
        Приготовление: Сварить бульон 1 час. Нарезать овощи. Обжарить лук и морковь.
        Добавить свёклу и капусту. Варить 20 минут.
        """

        try:
            result = await normalizer.normalize(test_text)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Live API ({normalizer.model}): {exc}")
            return

        print(f"✅ Live API ({normalizer.model})")
        print(f"✅ Название: {result['title']}")
        print(f"✅ meal_type: {result['meal_type']}")
        print(f"✅ difficulty: {result['difficulty']}")
        print(f"✅ cuisine: {result['cuisine']}")
        print(f"✅ Ингредиентов: {len(result['ingredients'])}")
        print(f"✅ Шагов: {len(result['steps'])}")
        print(f"✅ КБЖУ (на порцию): {result['nutrition_per_serving']}")
        print(f"✅ nutrition_note: {result.get('nutrition_note', '')!r}")
        print(f"✅ КБЖУ (на 100 г): {result['nutrition']}")
        print(
            f"✅ Время: prep={result['prep_time']} + cook={result['cook_time']} "
            f"= total={result['total_time']}"
        )

    asyncio.run(_test())
