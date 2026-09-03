"""
Скриптовые ответы на болтовню и FAQ — без вызова ИИ.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .intent_router import TextIntent

if TYPE_CHECKING:
    from config import Config


def _rate_label(seconds: int) -> str:
    sec = int(seconds or 0)
    if sec % 60 == 0 and sec >= 60:
        return f"{sec // 60} мин"
    return f"{sec} сек"


def reply_for_intent(intent: TextIntent, cfg: Optional["Config"] = None) -> Optional[str]:
    """
    Текст ответа для chitchat-интента.

    Returns:
        ``None`` для ``RECIPE`` и ``UNKNOWN`` — вызывающий код решает сам.
    """
    if intent == TextIntent.GREETING:
        return (
            "👋 Привет! Я Реми — помогу сохранить рецепт из ссылки или текста.\n\n"
            "Пришли ссылку на видео/сайт, вставь текст рецепта "
            "или нажми 📋 Меню."
        )

    if intent == TextIntent.THANKS:
        return (
            "🙏 Рад помочь! Если появится новый рецепт — просто пришли ссылку "
            "или текст, а сохранённые открой в 📖 Книге рецептов."
        )

    if intent == TextIntent.HELP:
        # Краткий FAQ; полная версия — /help и format_tutorial_text.
        from .handlers.commands import HELP_TEXT

        return HELP_TEXT

    if intent == TextIntent.LIMITS:
        if cfg is None:
            return (
                "⏱ Лимиты зависят от типа запроса (ссылка, фото, текст). "
                "Подробности — в /help или «📖 Инструкция» в меню /start."
            )
        url_sec = int(getattr(cfg, "url_rate_limit_seconds", 180) or 180)
        photo_sec = int(getattr(cfg, "photo_rate_limit_seconds", 120) or 120)
        text_sec = int(getattr(cfg, "text_rate_limit_seconds", 120) or 120)
        chef_sec = int(getattr(cfg, "chef_rate_limit_seconds", 0) or 0)
        max_vid = int(getattr(cfg, "max_video_duration_seconds", 120) or 120)
        vid_label = (
            f"{max_vid // 60} мин" if max_vid % 60 == 0 else f"{max_vid} сек"
        )
        lines = [
            "⏱ <b>Лимиты Реми</b>\n",
            f"• Ссылки — 1 раз в {_rate_label(url_sec)}",
            f"• Фото блюда — 1 раз в {_rate_label(photo_sec)}",
            f"• Текст рецепта — 1 раз в {_rate_label(text_sec)}",
        ]
        if chef_sec > 0:
            lines.append(f"• Вопрос шефу — 1 раз в {_rate_label(chef_sec)}")
        lines.extend([
            f"• Видео до {vid_label} — полный разбор",
            "",
            "Повторная ссылка из базы Remy (vault) лимит не тратит.",
        ])
        return "\n".join(lines)

    if intent == TextIntent.MINI_APP:
        webapp = (getattr(cfg, "webapp_url", None) or "").strip() if cfg else ""
        if webapp.startswith("https://"):
            return (
                "📖 <b>Книга рецептов</b> — твоя коллекция в Mini App.\n\n"
                "Открой через кнопку Menu у поля ввода или «📖 Книга рецептов» "
                "в меню /menu."
            )
        return (
            "📖 <b>Книга рецептов</b> появится, когда подключён Mini App.\n"
            "Пока смотри сохранённое через «📚 Сохранённые рецепты» в /menu."
        )

    if intent == TextIntent.OFFTOPIC:
        return (
            "🍳 Я заточен под рецепты: ссылки, текст, фото блюда.\n"
            "С этим не помогу — зато могу разобрать кулинарное видео или заметку."
        )

    return None
