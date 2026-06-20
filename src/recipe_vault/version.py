"""Версии пайплайна для инвалидации записей Recipe Vault."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import Config

# Bump при изменении yt-dlp / Whisper / лимита длины видео.
VAULT_PARSER_VERSION: str = "1"

# Bump при изменении промпта нормализатора (без смены модели).
VAULT_PROMPT_VERSION: str = "1"


def current_parser_version(max_video_duration_seconds: int) -> str:
    return f"p{VAULT_PARSER_VERSION}:dur{int(max_video_duration_seconds)}"


def current_normalize_version(cfg: "Config") -> str:
    model = str(getattr(cfg, "github_model", "") or "unknown").strip()
    effort = str(getattr(cfg, "github_reasoning_effort", "") or "medium").strip()
    return f"n{VAULT_PROMPT_VERSION}:{model}:{effort}"
