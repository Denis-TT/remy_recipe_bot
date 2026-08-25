"""
Recipe Vault — глобальное хранилище проверенных рецептов по URL.

Tiered retention:
  * ``draft`` — первый успешный парс, TTL (по умолчанию 90 дней);
  * ``golden`` — ``hit_count >= promote_hits`` → бессрочно (``expires_at IS NULL``).

Negative cache для неудач — короткий TTL (по умолчанию 12 ч).

In-memory coalescing: параллельные запросы одного ``cache_key`` ждут один пайплайн.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

from config import Config

from .keys import canonical_cache_key
from .models import (
    VaultFailureError,
    VaultFailureHit,
    VaultPipelineResult,
    VaultRecipeHit,
    recipe_from_vault_row,
)
from .version import current_normalize_version, current_parser_version

if False:  # TYPE_CHECKING without import cycle
    from ..storage.supabase_storage import SupabaseStorage

logger = logging.getLogger("remy.recipe_vault")

T = TypeVar("T")

PipelineFactory = Callable[[], Awaitable[VaultPipelineResult]]


class RecipeVault:
    """Фасад: lookup, coalescing, tier promotion, запись в Supabase."""

    def __init__(self, storage: "SupabaseStorage", cfg: Config) -> None:
        self._storage = storage
        self._cfg = cfg
        self._inflight: Dict[str, asyncio.Future[VaultPipelineResult]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "vault_enabled", True))

    def cache_key_for(self, url: str, source_type: str) -> str:
        return canonical_cache_key(url, source_type)

    def parser_version(self) -> str:
        return current_parser_version(self._cfg.max_video_duration_seconds)

    def normalize_version(self) -> str:
        return current_normalize_version(self._cfg)

    async def lookup(
        self,
        cache_key: str,
    ) -> Optional[VaultRecipeHit | VaultFailureHit]:
        """Проверить Vault без запуска пайплайна."""
        if not self.enabled:
            return None
        row = await self._storage.vault_get(cache_key)
        if row is None:
            return None
        if not self._row_is_active(row):
            return None
        if not self._version_matches(row):
            return None

        if row.get("is_failure"):
            reason = str(row.get("failure_reason") or "Не удалось обработать ссылку.")
            from .models import is_transient_llm_error

            if is_transient_llm_error(reason):
                logger.info(
                    "ℹ️ Recipe Vault: игнорирую устаревший временный failure: %s",
                    cache_key,
                )
                return None
            return VaultFailureHit(reason=reason)

        recipe = recipe_from_vault_row(row)
        if not recipe:
            return None

        bumped = await self._storage.vault_bump_hit(
            cache_key,
            promote_at=self._cfg.vault_promote_hits,
        )
        hit_count = int((bumped or row).get("hit_count") or 0)
        tier = str((bumped or row).get("tier") or "draft")
        logger.info(
            "⚡ Recipe Vault hit: %s (tier=%s, hits=%s)",
            cache_key,
            tier,
            hit_count,
        )
        return VaultRecipeHit(recipe=recipe, hit_count=hit_count, tier=tier)

    async def coalesce(
        self,
        cache_key: str,
        *,
        source_url: str,
        source_type: str,
        factory: PipelineFactory,
    ) -> VaultPipelineResult:
        """Выполнить пайплайн один раз на ключ; параллельные await-ят тот же результат."""
        if cache_key not in self._locks:
            self._locks[cache_key] = asyncio.Lock()

        leader = False
        fut: asyncio.Future[VaultPipelineResult]

        async with self._locks[cache_key]:
            existing = self._inflight.get(cache_key)
            if existing is not None:
                fut = existing
            else:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._inflight[cache_key] = fut
                leader = True

        if not leader:
            logger.info("⏳ Recipe Vault coalesce wait: %s", cache_key)
            return await fut

        try:
            result = await factory()
            if self.enabled:
                await self._persist_success(
                    cache_key,
                    source_url=source_url,
                    source_type=source_type,
                    result=result,
                )
            fut.set_result(result)
            return result
        except VaultFailureError as exc:
            if self.enabled and not exc.transient:
                await self._persist_failure(cache_key, source_url, source_type, exc)
            elif exc.transient:
                logger.info(
                    "ℹ️ Recipe Vault: временный сбой, без negative cache: %s",
                    cache_key,
                )
            fut.set_exception(exc)
            raise
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            async with self._locks[cache_key]:
                if self._inflight.get(cache_key) is fut:
                    self._inflight.pop(cache_key, None)

    async def _persist_success(
        self,
        cache_key: str,
        *,
        source_url: str,
        source_type: str,
        result: VaultPipelineResult,
    ) -> None:
        draft_until = datetime.now(timezone.utc) + timedelta(
            days=self._cfg.vault_draft_ttl_days,
        )
        image_url = str(result.image_url or "").strip()
        if image_url and not image_url.startswith(("http://", "https://")):
            image_url = ""

        payload = {
            "cache_key": cache_key,
            "source_type": source_type,
            "source_url": source_url.strip(),
            "raw_text": result.raw_text,
            "recipe_json": result.recipe,
            "image_url": image_url,
            "parser_version": self.parser_version(),
            "normalize_version": self.normalize_version(),
            "hit_count": 1,
            "tier": "draft",
            "is_failure": False,
            "failure_reason": "",
            "expires_at": draft_until.isoformat(),
            "last_hit_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._storage.vault_upsert(payload)
            logger.info("💾 Recipe Vault saved (draft): %s", cache_key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Recipe Vault save failed: %s", exc)

    async def _persist_failure(
        self,
        cache_key: str,
        source_url: str,
        source_type: str,
        exc: VaultFailureError,
    ) -> None:
        expires = datetime.now(timezone.utc) + timedelta(
            hours=self._cfg.vault_failure_ttl_hours,
        )
        payload = {
            "cache_key": cache_key,
            "source_type": source_type,
            "source_url": source_url.strip(),
            "raw_text": "",
            "recipe_json": {},
            "image_url": "",
            "parser_version": self.parser_version(),
            "normalize_version": self.normalize_version(),
            "hit_count": 0,
            "tier": "draft",
            "is_failure": True,
            "failure_reason": (exc.reason or exc.user_message)[:500],
            "expires_at": expires.isoformat(),
            "last_hit_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            await self._storage.vault_upsert(payload)
        except Exception as err:  # noqa: BLE001
            logger.warning("⚠️ Recipe Vault failure save: %s", err)

    def _version_matches(self, row: Dict[str, Any]) -> bool:
        return str(row.get("normalize_version") or "") == self.normalize_version()

    @staticmethod
    def _row_is_active(row: Dict[str, Any]) -> bool:
        expires = row.get("expires_at")
        if expires is None or expires == "":
            return True
        try:
            exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return exp > datetime.now(timezone.utc)
        except ValueError:
            return True


__all__ = [
    "RecipeVault",
    "VaultFailureError",
    "VaultPipelineResult",
    "VaultRecipeHit",
    "canonical_cache_key",
]
