"""Self-test Recipe Vault (без Supabase)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from config import Config


async def _run() -> None:
    from src.recipe_vault import RecipeVault, VaultFailureError, VaultPipelineResult
    from src.recipe_vault.keys import canonical_cache_key

    cfg = Config(
        telegram_token="t",
        github_token="g",
        supabase_url="https://example.supabase.co",
        supabase_key="k",
        vault_promote_hits=2,
        vault_draft_ttl_days=90,
        vault_failure_ttl_hours=12,
    )
    storage = MagicMock()
    storage.vault_get = AsyncMock(return_value=None)
    storage.vault_upsert = AsyncMock()
    storage.vault_bump_hit = AsyncMock(
        return_value={"hit_count": 2, "tier": "golden"},
    )

    vault = RecipeVault(storage, cfg)
    key = canonical_cache_key("https://youtu.be/abc123XYZ-_", "youtube")
    assert key == "yt:abc123XYZ-_"

    calls = 0

    async def factory() -> VaultPipelineResult:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return VaultPipelineResult(
            recipe={"title": "Test", "ingredients": [{}], "steps": []},
            raw_text="raw",
            image_url="https://cdn.example/img.jpg",
            source_type="youtube",
        )

    r1, r2 = await asyncio.gather(
        vault.coalesce(key, source_url="https://youtu.be/abc123XYZ-_", source_type="youtube", factory=factory),
        vault.coalesce(key, source_url="https://youtu.be/abc123XYZ-_", source_type="youtube", factory=factory),
    )
    assert calls == 1
    assert r1.recipe["title"] == "Test"
    assert r2.recipe["title"] == "Test"
    storage.vault_upsert.assert_called_once()

    storage.vault_get.return_value = {
        "cache_key": key,
        "is_failure": False,
        "normalize_version": vault.normalize_version(),
        "expires_at": None,
        "recipe_json": {"title": "Cached", "ingredients": [{}]},
        "hit_count": 1,
        "tier": "draft",
    }
    hit = await vault.lookup(key)
    assert hit is not None
    assert hit.recipe["title"] == "Cached"

    print("✅ Recipe Vault coalesce + lookup")


if __name__ == "__main__":
    asyncio.run(_run())
