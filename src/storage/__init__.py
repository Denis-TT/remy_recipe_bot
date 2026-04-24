"""Пакет хранилища рецептов Remy Bot.

Реэкспортирует публичные классы для удобного импорта:

    from src.storage import BaseStorage, SupabaseStorage
"""

from .base import BaseStorage
from .supabase_storage import SupabaseStorage

__all__ = ["BaseStorage", "SupabaseStorage"]
