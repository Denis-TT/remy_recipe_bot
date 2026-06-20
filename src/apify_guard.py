"""
Дневной лимит запусков Apify Actors (in-memory, один инстанс бота).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger("remy.apify_guard")

_guard: Optional["ApifyDailyGuard"] = None


class ApifyDailyGuard:
    """Не более ``max_runs_per_day`` POST /runs за календарный день (UTC)."""

    def __init__(self, max_runs_per_day: int) -> None:
        self.max_runs_per_day = max(0, int(max_runs_per_day))
        self._count = 0
        self._day = date.today()

    def try_acquire(self) -> bool:
        """Зарезервировать один run. ``max_runs_per_day=0`` — без лимита."""
        if self.max_runs_per_day <= 0:
            return True

        today = date.today()
        if today != self._day:
            self._day = today
            self._count = 0

        if self._count >= self.max_runs_per_day:
            logger.warning(
                "⚠️ Apify: дневной лимит исчерпан (%s/%s)",
                self._count,
                self.max_runs_per_day,
            )
            return False

        self._count += 1
        logger.info("Apify run %s/%s за сегодня", self._count, self.max_runs_per_day)
        return True


def configure_apify_guard(guard: Optional[ApifyDailyGuard]) -> None:
    global _guard
    _guard = guard


def get_apify_guard() -> Optional[ApifyDailyGuard]:
    return _guard
