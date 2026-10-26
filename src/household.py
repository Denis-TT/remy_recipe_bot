"""Общая кулинарная книга для нескольких Telegram-аккаунтов."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

    from config import Config
    from .bot import RemyBot

logger = logging.getLogger(__name__)

GUEST_GREETING = (
    "Настя, я Remy твой бот кулинар, отправляй мне ссылки на рецепты "
    "чтобы я сохранил их в вашу кулинарную книгу и муж тебе это готовил:)"
)

_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "household_greeted.json"
_greeted: set[int] = set()
_loaded = False


def normalize_username(username: str | None) -> str:
    return (username or "").strip().lstrip("@").lower()


def book_user_id(cfg: "Config", user_id: int, username: str | None = None) -> int:
    """Чей `user_id` писать в recipes. Гость семьи пишет в книгу владельца."""
    owner = int(getattr(cfg, "cookbook_owner_id", 0) or 0)
    uid = int(user_id)
    if owner <= 0:
        return uid
    if uid == owner:
        return owner
    names = getattr(cfg, "household_usernames", ()) or ()
    if normalize_username(username) in names:
        return owner
    return uid


def is_household_guest(cfg: "Config", user_id: int, username: str | None = None) -> bool:
    owner = int(getattr(cfg, "cookbook_owner_id", 0) or 0)
    if owner <= 0 or int(user_id) == owner:
        return False
    return book_user_id(cfg, user_id, username) == owner


def _load_greeted() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    ids = raw.get("user_ids") if isinstance(raw, dict) else None
    if isinstance(ids, list):
        for item in ids:
            try:
                _greeted.add(int(item))
            except (TypeError, ValueError):
                continue


def _save_greeted() -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"user_ids": sorted(_greeted)}
    _STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")


async def note_and_greet(
    bot: "RemyBot",
    context: "ContextTypes.DEFAULT_TYPE",
    user: Any,
) -> None:
    """Записать гостя в общую книгу и один раз отправить приветствие."""
    if user is None or not is_household_guest(bot.config, user.id, user.username):
        return

    _load_greeted()
    uid = int(user.id)
    try:
        await bot.storage.upsert_household_member(
            uid,
            book_user_id(bot.config, uid, user.username),
            normalize_username(user.username),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Не удалось записать household member %s: %s", uid, exc)

    if uid in _greeted:
        return
    _greeted.add(uid)
    try:
        _save_greeted()
    except OSError as exc:
        logger.warning("⚠️ Не удалось сохранить флаг приветствия: %s", exc)
    try:
        await context.bot.send_message(chat_id=uid, text=GUEST_GREETING)
        logger.info("✉️ Приветствие общей книги отправлено user %s", uid)
    except Exception as exc:  # noqa: BLE001
        _greeted.discard(uid)
        logger.warning("⚠️ Не удалось отправить приветствие user %s: %s", uid, exc)


if __name__ == "__main__":
    class _Cfg:
        cookbook_owner_id = 10
        household_usernames = ("nany_hodanovich",)

    assert book_user_id(_Cfg(), 10, "dennis") == 10
    assert book_user_id(_Cfg(), 20, "Nany_Hodanovich") == 10
    assert book_user_id(_Cfg(), 30, "other") == 30
    assert is_household_guest(_Cfg(), 20, "@nany_hodanovich")
    assert not is_household_guest(_Cfg(), 10, "dennis")
    print("✅ household")
