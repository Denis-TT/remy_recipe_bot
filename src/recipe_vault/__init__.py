"""Recipe Vault — глобальная база распарсенных URL."""

from .keys import canonical_cache_key
from .models import VaultFailureError, VaultPipelineResult, VaultRecipeHit
from .vault import RecipeVault

__all__ = [
    "RecipeVault",
    "VaultFailureError",
    "VaultPipelineResult",
    "VaultRecipeHit",
    "canonical_cache_key",
]
