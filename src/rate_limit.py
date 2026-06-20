"""
Ограничение частоты запросов на пользователя (in-memory, один инстанс бота).
"""

from __future__ import annotations

import time
from typing import Dict, Tuple


class UserRateLimiter:
    """Не чаще одного события на пользователя за заданный интервал."""

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._last_at: Dict[int, float] = {}

    def check(self, user_id: int) -> Tuple[bool, int]:
        """Проверить, можно ли обрабатывать запрос сейчас.

        Returns:
            ``(allowed, seconds_to_wait)`` — если ``allowed`` ложно, во втором
            элементе — сколько секунд подождать (округление вверх).
        """
        if self.interval_seconds <= 0:
            return True, 0

        now = time.monotonic()
        last = self._last_at.get(int(user_id))
        if last is None:
            return True, 0

        elapsed = now - last
        if elapsed >= self.interval_seconds:
            return True, 0

        wait = int(self.interval_seconds - elapsed) + 1
        return False, max(1, wait)

    def record(self, user_id: int) -> None:
        """Зафиксировать начало обработки запроса."""
        if self.interval_seconds <= 0:
            return
        self._last_at[int(user_id)] = time.monotonic()


def format_wait_label(interval_seconds: int) -> str:
    """Человекочитаемый интервал для сообщения пользователю."""
    sec = int(interval_seconds)
    if sec % 60 == 0 and sec >= 60:
        return f"{sec // 60} мин"
    return f"{sec} сек"


if __name__ == "__main__":
    limiter = UserRateLimiter(180)
    ok, wait = limiter.check(42)
    assert ok and wait == 0
    limiter.record(42)
    ok2, wait2 = limiter.check(42)
    assert not ok2 and wait2 > 0
    print("✅ UserRateLimiter")
