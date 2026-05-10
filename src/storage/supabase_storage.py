"""
Реализация хранилища рецептов поверх Supabase REST API (PostgREST).

Работает через прямые HTTP-запросы `aiohttp` — без зависимости
от `supabase-py`. Это даёт:

* полный контроль над формой запросов и заголовков;
* меньшее число транзитивных зависимостей;
* единый стиль логирования/обработки ошибок со всем проектом.

Формат таблицы описан в `sql/create_tables.sql`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Tuple

import aiohttp

from ..localization import Localization
from .base import BaseStorage


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

#: Колонки таблицы `recipes`, в которые разрешена запись из приложения.
#: Всё, что не входит в этот набор (`id`, `created_at`, `updated_at`,
#: случайные поля от AI-модели), отбрасывается перед POST-запросом.
_ALLOWED_COLUMNS = frozenset({
    "user_id",
    "title", "description",
    "cuisine", "meal_type", "dish_type", "main_ingredient", "difficulty",
    "prep_time", "cook_time", "total_time", "servings",
    "ingredients", "steps",
    "nutrition", "nutrition_per_serving", "total_nutrition",
    "equipment", "tips", "tags",
    "storage",
    "is_vegetarian", "is_vegan", "is_gluten_free", "is_lactose_free",
    "source_url",
    "image_url",
})

#: Таймаут одного HTTP-запроса (секунды). Supabase обычно отвечает
#: за десятки миллисекунд, но на холодном старте региона бывает до 5 с.
_TIMEOUT_SECONDS = 30.0

#: Символы, которые могут разрушить синтаксис PostgREST в `or=(...)`.
#: Вырезаем их из пользовательского поискового запроса.
_POSTGREST_UNSAFE_CHARS = str.maketrans({c: " " for c in "(),"})


# --------------------------------------------------------------------------- #
# Реализация хранилища
# --------------------------------------------------------------------------- #

class SupabaseStorage(BaseStorage):
    """Хранилище рецептов на Supabase REST API.

    Attributes:
        url: Базовый URL проекта, например ``https://xxx.supabase.co``.
        key: Публичный `anon` ключ (или service-role — на ваше усмотрение).
        rest_base: Готовый префикс всех REST-запросов
            (``{url}/rest/v1``).
    """

    def __init__(self, url: str, key: str) -> None:
        """Инициализация клиента.

        Args:
            url: Полный URL Supabase-проекта. Завершающий слэш не обязателен.
            key: Supabase API key.

        Raises:
            ValueError: если `url` или `key` пусты.
        """
        if not url or not url.strip():
            raise ValueError("SUPABASE_URL обязателен")
        if not key or not key.strip():
            raise ValueError("SUPABASE_KEY обязателен")

        self.url: str = url.rstrip("/")
        self.key: str = key
        self.rest_base: str = f"{self.url}/rest/v1"

        logger.info("🚀 Подключение к Supabase: %s", self._mask_url(self.url))

    # ------------------------------------------------------------------ #
    # CRUD: save
    # ------------------------------------------------------------------ #

    async def save_recipe(
        self, user_id: int, recipe_data: Mapping[str, Any]
    ) -> Dict[str, Any]:
        """Сохранить новый рецепт в таблицу ``recipes``.

        Перед отправкой выполняется:
        1. `Localization.normalize_recipe` — чтобы `meal_type`, `dish_type`,
           `main_ingredient`, `difficulty` и `cuisine` хранились в
           канонической латинице.
        2. Фильтрация полей по `_ALLOWED_COLUMNS` — лишние ключи
           (например, сгенерированные AI-моделью) отбрасываются, чтобы
           PostgREST не ответил ошибкой 400 «unknown column».
        3. Принудительная установка `user_id` из аргумента — клиент
           НИКОГДА не доверяет `user_id` из тела `recipe_data`.

        Returns:
            Созданная запись со всеми серверными полями (`id`,
            `created_at`, ...).
        """
        normalized = Localization.normalize_recipe(dict(recipe_data))

        payload = {k: v for k, v in normalized.items() if k in _ALLOWED_COLUMNS}
        payload["user_id"] = int(user_id)

        _, body = await self._request(
            "POST", "/recipes",
            json_body=[payload],
            extra_headers={"Prefer": "return=representation"},
        )

        rows = self._parse_json_body(body)
        if not isinstance(rows, list) or not rows:
            logger.error("❌ Supabase не вернул созданную запись (тело: %s)", body[:300])
            raise RuntimeError("Supabase не вернул созданную запись")

        saved = rows[0]
        logger.info(
            "✅ Рецепт «%s» сохранён (meal_type=%s, id=%s)",
            saved.get("title", ""),
            saved.get("meal_type", ""),
            saved.get("id", ""),
        )
        return saved

    # ------------------------------------------------------------------ #
    # CRUD: read
    # ------------------------------------------------------------------ #

    async def get_recipe(self, recipe_id: str) -> Optional[Dict[str, Any]]:
        """Получить рецепт по его UUID.

        Returns:
            Словарь рецепта или `None`, если не найден.
        """
        params = {
            "id": f"eq.{recipe_id}",
            "limit": "1",
        }
        _, body = await self._request("GET", "/recipes", params=params)
        rows = self._parse_json_body(body)

        if not isinstance(rows, list) or not rows:
            logger.info("📄 Рецепт %s не найден", recipe_id)
            return None

        logger.info("📄 Загружен рецепт %s", recipe_id)
        return rows[0]

    async def get_user_recipes(
        self,
        user_id: int,
        meal_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Получить список рецептов пользователя, новые — первыми."""
        params: Dict[str, str] = {
            "user_id": f"eq.{int(user_id)}",
            "order": "created_at.desc",
            "limit": str(int(limit)),
        }
        if meal_type:
            params["meal_type"] = f"eq.{meal_type}"

        _, body = await self._request("GET", "/recipes", params=params)
        rows = self._parse_json_body(body)
        if not isinstance(rows, list):
            rows = []

        logger.info(
            "📖 Загружено %d рецептов (user=%d%s)",
            len(rows),
            int(user_id),
            f", meal_type={meal_type}" if meal_type else "",
        )
        return rows

    async def get_categories(self, user_id: int) -> List[Dict[str, Any]]:
        """Сгруппировать рецепты пользователя по `meal_type`.

        Returns:
            Список ``[{"key": "lunch", "count": 5}, ...]``,
            отсортированный по убыванию `count`.
        """
        params = {
            "user_id": f"eq.{int(user_id)}",
            "select": "meal_type",
        }
        _, body = await self._request("GET", "/recipes", params=params)
        rows = self._parse_json_body(body)
        if not isinstance(rows, list):
            rows = []

        counts: Dict[str, int] = {}
        for row in rows:
            key = (row.get("meal_type") if isinstance(row, dict) else None) or "other"
            counts[key] = counts.get(key, 0) + 1

        categories = [
            {"key": key, "count": count}
            for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ]

        summary = ", ".join(f"{c['key']} ({c['count']})" for c in categories) or "пусто"
        logger.info("📂 Категории user=%d: %s", int(user_id), summary)
        return categories

    async def search_recipes(
        self, user_id: int, query: str
    ) -> List[Dict[str, Any]]:
        """Поиск по подстроке в `title` или `description`.

        Пользовательский ввод экранируется: символы ``( ) ,`` —
        разделители синтаксиса PostgREST ``or=(...)`` — заменяются
        на пробел, чтобы они не сломали запрос.
        """
        safe = (query or "").translate(_POSTGREST_UNSAFE_CHARS).strip()
        if not safe:
            logger.info("🔎 Пустой поисковый запрос (user=%d)", int(user_id))
            return []

        pattern = f"*{safe}*"
        params = {
            "user_id": f"eq.{int(user_id)}",
            "or": f"(title.ilike.{pattern},description.ilike.{pattern})",
            "order": "created_at.desc",
            "limit": "50",
        }
        _, body = await self._request("GET", "/recipes", params=params)
        rows = self._parse_json_body(body)
        if not isinstance(rows, list):
            rows = []

        logger.info(
            "🔎 Поиск «%s» (user=%d): найдено %d рецептов",
            safe, int(user_id), len(rows),
        )
        return rows

    # ------------------------------------------------------------------ #
    # CRUD: delete
    # ------------------------------------------------------------------ #

    async def delete_recipe(self, recipe_id: str, user_id: int) -> bool:
        """Удалить рецепт по `id`, если он принадлежит `user_id`."""
        params = {
            "id": f"eq.{recipe_id}",
            "user_id": f"eq.{int(user_id)}",
        }
        _, body = await self._request(
            "DELETE", "/recipes",
            params=params,
            extra_headers={"Prefer": "return=representation"},
        )

        try:
            rows = self._parse_json_body(body) if body.strip() else []
        except RuntimeError:
            rows = []

        deleted = isinstance(rows, list) and len(rows) > 0
        if deleted:
            logger.info("🗑️  Рецепт %s удалён (user=%d)", recipe_id, int(user_id))
        else:
            logger.warning(
                "⚠️  Рецепт %s не найден или не принадлежит user=%d",
                recipe_id, int(user_id),
            )
        return deleted

    # ------------------------------------------------------------------ #
    # Служебные методы
    # ------------------------------------------------------------------ #

    async def health_check(self) -> bool:
        """Проверить доступность Supabase.

        Делает `HEAD` по `/rest/v1/recipes?limit=1`. Не бросает
        исключений: любые сетевые/аутентификационные ошибки
        превращаются в `False` и подробно логируются.
        """
        try:
            status, _ = await self._request(
                "HEAD", "/recipes", params={"limit": "1"}
            )
        except Exception as exc:  # noqa: BLE001 — намеренно широкий catch
            logger.error("❌ Health check Supabase не прошёл: %s", exc)
            return False

        ok = status == 200
        if ok:
            logger.info("✅ Подключение к Supabase установлено")
        else:
            logger.warning("⚠️  Health check Supabase: неожиданный статус %d", status)
        return ok

    # ------------------------------------------------------------------ #
    # Внутренности: HTTP
    # ------------------------------------------------------------------ #

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        """Собрать стандартные заголовки запроса к Supabase."""
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, str]] = None,
        json_body: Any = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Tuple[int, str]:
        """Выполнить HTTP-запрос к Supabase REST API.

        Раскладывает все сетевые и серверные ошибки в понятные
        `RuntimeError` с русскими сообщениями, чтобы бизнес-слой
        не разбирался с деталями aiohttp.

        Returns:
            Кортеж ``(status, body_text)``. Для 2xx `body_text` может
            быть пустой строкой (например, у HEAD).
        """
        url = f"{self.rest_base}{path}"
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=self._headers(extra_headers),
                ) as response:
                    body = await response.text(errors="replace")

                    if response.status in (401, 403):
                        logger.error(
                            "❌ Ошибка аутентификации Supabase (%d): %s",
                            response.status, self._truncate(body, 300),
                        )
                        raise RuntimeError("Ошибка аутентификации Supabase")

                    if response.status >= 400:
                        logger.error(
                            "❌ Supabase вернул HTTP %d на %s %s: %s",
                            response.status, method, path,
                            self._truncate(body, 300),
                        )
                        raise RuntimeError(
                            f"Supabase вернул HTTP {response.status}"
                        )

                    return response.status, body

        except asyncio.TimeoutError:
            logger.error("❌ Таймаут при обращении к Supabase (%s %s)", method, path)
            raise RuntimeError("Таймаут при обращении к Supabase") from None
        except aiohttp.ClientError as exc:
            logger.error("❌ Ошибка подключения к Supabase: %s", exc)
            raise RuntimeError(f"Ошибка подключения к Supabase: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Внутренности: утилиты
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_json_body(body: str) -> Any:
        """Распарсить тело ответа Supabase.

        Пустое тело считается ошибкой: все обращения, использующие
        эту функцию, обязаны возвращать JSON (массив или объект).
        """
        text = (body or "").strip()
        if not text:
            raise RuntimeError("Пустое тело ответа от Supabase")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Некорректный JSON от Supabase: {exc} (начало тела: {text[:200]!r})"
            ) from exc

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Укоротить строку для логирования."""
        if text is None:
            return ""
        if len(text) <= limit:
            return text
        return text[:limit] + "…"

    @staticmethod
    def _mask_url(url: str) -> str:
        """Укоротить URL проекта для логов: оставить схему + хост."""
        try:
            from urllib.parse import urlsplit

            parts = urlsplit(url)
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
        except Exception:  # noqa: BLE001
            pass
        return url


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
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_KEY", "")

        # Плейсхолдеры из .env.example → пропускаем сетевой тест
        placeholders = {"", "your_supabase_url_here", "your_supabase_anon_key_here"}
        if url in placeholders or key in placeholders:
            print(
                "⚠️  SUPABASE_URL/SUPABASE_KEY не заданы (или это плейсхолдеры) — "
                "пропускаю интеграционный тест"
            )
            assert "image_url" in _ALLOWED_COLUMNS
            print("✅ Колонка image_url разрешена в save_recipe (_ALLOWED_COLUMNS)")
            return

        storage = SupabaseStorage(url, key)

        if not await storage.health_check():
            print("❌ Health check не прошёл — пропускаю остальные проверки")
            return
        print("✅ Подключение к Supabase работает")

        test_recipe = {
            "title": "Тестовый рецепт",
            "meal_type": "обед",
            "difficulty": "легко",
            "cuisine": "итальянская",
            "ingredients": [{"name": "Тест", "amount": 100, "unit": "г"}],
            "steps": [{"step_number": 1, "description": "Тестовый шаг"}],
            "nutrition_per_serving": {
                "calories": 100, "protein": 5, "fat": 3, "carbs": 15,
            },
            "image_url": "https://example.com/recipe-hero.jpg",
        }

        saved = await storage.save_recipe(12345, test_recipe)
        print(f"✅ Сохранён рецепт: {saved.get('id')}")
        print(f"✅ meal_type в БД: {saved.get('meal_type')}")  # ожидаем 'lunch'
        img_stored = saved.get("image_url")
        if img_stored != test_recipe["image_url"]:
            print(f"⚠️  image_url в ответе сохранения: ожидалось {test_recipe['image_url']!r}, получено {img_stored!r}")
        else:
            print(f"✅ image_url сохранён: {img_stored}")

        fetched = await storage.get_recipe(saved["id"])
        assert fetched is not None and fetched["id"] == saved["id"]
        print("✅ get_recipe вернул запись")

        cats = await storage.get_categories(12345)
        print(f"✅ Категории: {cats}")

        found = await storage.search_recipes(12345, "тестовый")
        print(f"✅ Поиск нашёл: {len(found)} рецептов")

        deleted = await storage.delete_recipe(saved["id"], 12345)
        print(f"✅ Удаление: {deleted}")

    asyncio.run(_test())
