"""
Абстрактный базовый класс хранилища рецептов Remy Bot.

Определяет единый контракт, которому обязаны соответствовать все
реализации — `SupabaseStorage` (Блок 5), а в будущем, например,
`PostgresStorage` или `InMemoryStorage` для тестов.

Все методы асинхронные: реальное хранилище живёт за сетью, а
в-памяти-реализации всё равно должны соответствовать этой сигнатуре,
чтобы быть взаимозаменяемыми.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class BaseStorage(ABC):
    """Абстрактное хранилище рецептов.

    Наследники обязаны реализовать CRUD и служебные операции:
    сохранение, получение по id, получение всех рецептов пользователя,
    выборка категорий, поиск, удаление, проверка работоспособности.

    Единицей хранения выступает рецепт — `dict` с полями, описанными
    в `sql/create_tables.sql`. Ключи `meal_type`, `difficulty`,
    `cuisine` должны храниться в «канонической латинице» (см.
    `src.localization.Localization.normalize_recipe`).
    """

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def save_recipe(
        self, user_id: int, recipe_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Сохранить новый рецепт и вернуть сохранённую запись.

        Реализация обязана нормализовать `recipe_data` через
        `Localization.normalize_recipe`, чтобы в БД попадали только
        канонические латинские ключи для `meal_type`/`difficulty`/
        `cuisine`.

        Args:
            user_id: Telegram ID пользователя-владельца.
            recipe_data: Словарь полей рецепта (без `id` и `user_id`).

        Returns:
            Созданная запись со всеми полями БД (включая `id`,
            `created_at` и т. п.).

        Raises:
            RuntimeError: при сетевой/серверной ошибке хранилища.
        """

    @abstractmethod
    async def get_recipe(self, recipe_id: str) -> Optional[Dict[str, Any]]:
        """Получить один рецепт по его идентификатору.

        Args:
            recipe_id: Строковый UUID рецепта.

        Returns:
            Словарь рецепта или `None`, если не найден.

        Raises:
            RuntimeError: при сетевой/серверной ошибке.
        """

    @abstractmethod
    async def get_user_recipes(
        self,
        user_id: int,
        meal_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Получить рецепты пользователя, отсортированные по дате создания (новые — первые).

        Args:
            user_id: Telegram ID пользователя.
            meal_type: Опциональный фильтр по типу блюда
                (значение из `Localization.VALID_MEAL_TYPES`).
            limit: Максимальное число записей.

        Returns:
            Список словарей-рецептов (может быть пустым).
        """

    @abstractmethod
    async def get_categories(self, user_id: int) -> List[Dict[str, Any]]:
        """Получить сводку по категориям пользователя.

        Args:
            user_id: Telegram ID пользователя.

        Returns:
            Список словарей вида ``{"key": "lunch", "count": 5}``,
            отсортированный по убыванию `count`.
        """

    @abstractmethod
    async def search_recipes(
        self, user_id: int, query: str
    ) -> List[Dict[str, Any]]:
        """Полнотекстово-подобный поиск рецептов пользователя.

        Ищет по полям `title` и `description` (регистронезависимо,
        подстрока).

        Args:
            user_id: Telegram ID пользователя.
            query: Поисковая строка.

        Returns:
            Список найденных рецептов.
        """

    @abstractmethod
    async def delete_recipe(self, recipe_id: str, user_id: int) -> bool:
        """Удалить рецепт пользователя.

        Удаление должно быть безопасным: рецепт удаляется, только если
        `user_id` совпадает с владельцем.

        Args:
            recipe_id: UUID рецепта.
            user_id: Telegram ID владельца.

        Returns:
            `True`, если запись действительно была удалена;
            `False`, если подходящих записей не нашлось.
        """

    # ------------------------------------------------------------------ #
    # Служебное
    # ------------------------------------------------------------------ #

    @abstractmethod
    async def health_check(self) -> bool:
        """Проверить, что хранилище доступно и отвечает.

        Returns:
            `True`, если хранилище отвечает штатно; `False`
            при любых сетевых/аутентификационных проблемах.
            Метод НЕ должен бросать исключения — это health-check.
        """
