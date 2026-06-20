"""Модели данных Recipe Vault."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class VaultRecipeHit:
    """Готовый рецепт из Vault (L2 hit)."""

    recipe: dict[str, Any]
    hit_count: int
    tier: str


@dataclass(frozen=True)
class VaultFailureHit:
    """Negative cache — недавняя неудачная обработка."""

    reason: str


@dataclass
class VaultPipelineResult:
    """Результат полного пайплайна parse → normalize (до записи в Vault)."""

    recipe: dict[str, Any]
    raw_text: str
    image_url: str
    source_type: str


class VaultFailureError(Exception):
    """Пользовательская ошибка, которую можно положить в negative cache."""

    def __init__(self, user_message: str, *, reason: str = "") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.reason = reason or user_message


def recipe_from_vault_row(row: Mapping[str, Any]) -> dict[str, Any]:
    data = row.get("recipe_json")
    if isinstance(data, dict):
        return dict(data)
    return {}
