"""
Хендлеры Remy Bot.

Пакет содержит асинхронные функции-обработчики Telegram-апдейтов,
сгруппированные по типам:

* :mod:`commands` — `/start`, `/menu`, `/help`;
* :mod:`messages` — произвольные текстовые сообщения, URL, фото (vision);
* :mod:`callbacks` — нажатия на inline-кнопки.

Все хендлеры доступ к общей инфраструктуре (парсер, нормализатор,
хранилище, локализация, временный кэш рецептов) получают через
``context.application.bot_data["remy"]`` — там лежит экземпляр
:class:`src.bot.RemyBot`. Такая схема дешевле и чище, чем передача
зависимостей через замыкания: хендлеры остаются идиоматичными для PTB
(`async def handler(update, context)`), а тесты легко подкладывают
любой объект в `bot_data`.
"""

from __future__ import annotations

from . import callbacks, commands, messages

__all__ = ["callbacks", "commands", "messages"]
