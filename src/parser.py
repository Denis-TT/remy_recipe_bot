"""
Модуль парсеров рецептов Remy Bot.

Определяет расширяемую архитектуру для извлечения сырого текста
рецепта из разных источников:

* `BaseParser` — абстрактный базовый класс, описывающий контракт
  (методы `can_parse`, `parse`, свойство `source_type`).
* `WebParser` — конкретная реализация для обычных веб-страниц
  (HTTP(S)-сайты с рецептами).
* `YouTubeParser` — YouTube: субтитры (и опционально заголовок/описание
  через YouTube Data API v3) → сырой текст для нормализатора.
* `ParserRegistry` — реестр парсеров, который автоматически
  выбирает подходящий парсер по URL.
* `create_parser_registry` — фабрика, возвращающая реестр с
  предустановленным набором парсеров.

В дальнейшем к иерархии могут быть добавлены, например,
`InstagramReelParser`, `TikTokParser` — им достаточно унаследоваться
от `BaseParser` и быть зарегистрированными в `ParserRegistry`.

Возвращаемый текст намеренно сырой и некрасивый — дальнейшая
нормализация и извлечение структурированных полей (заголовок,
ингредиенты, шаги) делается в отдельном слое (AI/LLM-обработка).
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup

try:
    # readability-lxml даёт гораздо более чистый текст на типичных
    # блогах/порталах с рецептами. Если пакет недоступен — мы всё
    # равно отработаем, только с более шумным результатом.
    from readability import Document as _ReadabilityDocument
except Exception:  # noqa: BLE001 — нам неважно, что именно сломалось
    _ReadabilityDocument = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Абстрактный базовый класс
# --------------------------------------------------------------------------- #

class BaseParser(ABC):
    """Абстрактный базовый класс для всех парсеров рецептов.

    Наследники обязаны реализовать три точки расширения:

    * `source_type` — строковый идентификатор источника
      (например, ``"website"``, ``"youtube_shorts"``).
    * `can_parse(url)` — быстрая проверка, подходит ли URL для
      данного парсера (используется реестром для маршрутизации).
    * `parse(url)` — собственно асинхронное извлечение текста.
    """

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Тип источника.

        Ожидаемые значения: ``"website"``, ``"youtube_shorts"``,
        ``"instagram_reel"``, ``"tiktok"``. Используется в логах и,
        потенциально, при сохранении рецепта — чтобы знать, откуда он.
        """

    @staticmethod
    @abstractmethod
    def can_parse(url: str) -> bool:
        """Вернуть True, если данный парсер умеет обработать `url`.

        Должен быть быстрым и дешёвым: сетевые запросы здесь
        запрещены. Обычно это проверка схемы/домена URL.
        """

    @abstractmethod
    async def parse(self, url: str) -> str:
        """Извлечь сырой текст рецепта из источника.

        Args:
            url: URL источника.

        Returns:
            Извлечённый текст (может содержать переводы строк).

        Raises:
            ValueError: если URL не поддерживается этим парсером.
            RuntimeError: при ошибке сети/парсинга (таймаут, HTTP-ошибка,
                пустой контент и т. п.).
        """


# --------------------------------------------------------------------------- #
# Реализация: обычные веб-страницы
# --------------------------------------------------------------------------- #

class WebParser(BaseParser):
    """Парсер обычных веб-страниц с рецептами.

    Алгоритм:

    1. Асинхронный GET-запрос с браузерным User-Agent и таймаутом 30 с.
    2. Извлечение основного содержимого через `readability-lxml`
       (с фолбэком на чистый HTML, если readability не справился).
    3. Очистка `BeautifulSoup`: удаление `<script>`, `<style>`, `<nav>`,
       `<footer>`, `<header>`, `<aside>`, `<form>`, `<iframe>`,
       `<noscript>` и сворачивание пробелов.
    4. Обрезка результата до `MAX_TEXT_LENGTH` символов.
    """

    #: Строковый идентификатор источника.
    source_type: str = "website"

    #: Максимальная длина возвращаемого текста (символы).
    MAX_TEXT_LENGTH: int = 50_000

    #: Таймаут HTTP-запроса в секундах.
    TIMEOUT_SECONDS: float = 30.0

    #: HTTP-заголовки, имитирующие обычный браузер Chrome.
    HEADERS: dict = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }

    #: HTML-теги, которые точно не содержат полезного текста рецепта.
    _TAGS_TO_REMOVE: tuple = (
        "script", "style", "nav", "footer", "header",
        "aside", "form", "iframe", "noscript", "svg",
    )

    # ------------------------------------------------------------------ #
    # Точки расширения из BaseParser
    # ------------------------------------------------------------------ #

    @staticmethod
    def can_parse(url: str) -> bool:
        """Проверить, похож ли URL на обычную HTTP(S)-страницу.

        Специально делаем проверку самой общей: `WebParser` должен
        использоваться как fallback для всех URL, не перехваченных
        более специализированными парсерами (YouTube, Instagram и др.).
        Поэтому реестр должен регистрировать `WebParser` последним.
        """
        if not isinstance(url, str):
            return False
        return url.startswith(("http://", "https://"))

    async def parse(self, url: str) -> str:
        """Загрузить страницу и вернуть очищенный текст рецепта.

        Args:
            url: Полный HTTP(S)-URL страницы.

        Returns:
            Текст длиной до `MAX_TEXT_LENGTH` символов.

        Raises:
            ValueError: если URL не поддерживается.
            RuntimeError: при таймауте, HTTP-ошибке или пустом контенте.
        """
        if not self.can_parse(url):
            raise ValueError(f"WebParser не поддерживает URL: {url!r}")

        logger.info("🔍 Начинаю парсинг: %s", url)

        html = await self._fetch(url)
        text = self._extract_text(html)

        if not text:
            logger.error("❌ Ошибка парсинга %s: пустой контент", url)
            raise RuntimeError("Страница не содержит текста")

        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]

        logger.info("📄 Извлечено %s символов текста", self._format_number(len(text)))
        return text

    # ------------------------------------------------------------------ #
    # Внутренняя реализация
    # ------------------------------------------------------------------ #

    async def _fetch(self, url: str) -> str:
        """Выполнить HTTP GET и вернуть тело ответа как строку.

        Преобразует сетевые ошибки и нештатные HTTP-статусы в
        `RuntimeError` с понятными русскими сообщениями.
        """
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=self.HEADERS) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status >= 400:
                        logger.error(
                            "❌ Ошибка парсинга %s: HTTP %s", url, response.status
                        )
                        raise RuntimeError(
                            f"Ошибка HTTP {response.status} при загрузке страницы"
                        )

                    # `response.text()` сам подбирает кодировку из заголовков,
                    # а при их отсутствии — через chardet.
                    html = await response.text(errors="replace")

                    size_kb = max(len(html) // 1024, 1)
                    logger.info(
                        "✅ Страница загружена (%s), размер: %sKB",
                        response.status,
                        size_kb,
                    )
                    return html

        except asyncio.TimeoutError:
            logger.error("❌ Ошибка парсинга %s: таймаут соединения", url)
            raise RuntimeError("Таймаут при загрузке страницы") from None
        except aiohttp.ClientError as exc:
            logger.error("❌ Ошибка парсинга %s: %s", url, exc)
            raise RuntimeError(f"Сетевая ошибка: {exc}") from exc

    def _extract_text(self, html: str) -> str:
        """Извлечь основной текст страницы из HTML.

        Сначала пытаемся выделить «статью» через readability-lxml —
        он хорошо отсекает меню, сайдбары, рекламу. Если readability
        по каким-то причинам упал или не установлен, работаем напрямую
        с исходным HTML.
        """
        content_html = html

        if _ReadabilityDocument is not None:
            try:
                doc = _ReadabilityDocument(html)
                summary = doc.summary(html_partial=True)
                if summary and summary.strip():
                    content_html = summary
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "⚠️  readability не справился, используем полный HTML: %s", exc
                )

        soup = BeautifulSoup(content_html, "lxml")

        for tag in soup(self._TAGS_TO_REMOVE):
            tag.decompose()

        raw_text = soup.get_text(separator="\n")
        return self._clean_text(raw_text)

    @staticmethod
    def _clean_text(text: str) -> str:
        """Нормализовать пробелы и убрать пустые строки.

        * схлопывает повторяющиеся пробелы/табы в один пробел;
        * убирает ведущие/концевые пробелы в каждой строке;
        * выкидывает пустые строки;
        * схлопывает более двух переводов строк подряд в два.
        """
        lines: Iterable[str] = text.splitlines()
        cleaned_lines: List[str] = []
        for line in lines:
            collapsed = re.sub(r"[ \t\u00a0]+", " ", line).strip()
            if collapsed:
                cleaned_lines.append(collapsed)

        return "\n".join(cleaned_lines)

    @staticmethod
    def _format_number(value: int) -> str:
        """Отформатировать число с пробелом-разделителем тысяч (``12 345``)."""
        return f"{value:,}".replace(",", " ")


# --------------------------------------------------------------------------- #
# Реализация: YouTube (субтитры + опционально Data API: заголовок/описание)
# --------------------------------------------------------------------------- #

def _extract_youtube_video_id(url: str) -> str:
    """Вернуть 11-символьный video_id или пустую строку (watch, Shorts, youtu.be)."""
    s = (url or "").strip()
    m = re.search(
        r"(?:[?&]v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})\b",
        s,
        re.IGNORECASE,
    )
    if m:
        return m.group(1)
    return ""


def _youtube_list_transcripts(video_id: str) -> Any:
    """Совместимость youtube-transcript-api 0.6.x (list_transcripts) и 1.x (list)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    if hasattr(YouTubeTranscriptApi, "list_transcripts"):
        return YouTubeTranscriptApi.list_transcripts(video_id)  # type: ignore[no-any-return, misc]
    ytt = YouTubeTranscriptApi()  # type: ignore[call-arg]
    return ytt.list(video_id)


def _flatten_subtitle_fetch(data: Any) -> str:
    """Склеить субтитры в одну строку (list[dict] или FetchedTranscript)."""
    if not data:
        return ""
    if isinstance(data, list):
        parts: List[str] = []
        for item in data:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]).strip())
        return re.sub(r"\s+", " ", " ".join(parts)).strip()
    to_raw = getattr(data, "to_raw_data", None)
    if callable(to_raw):
        return _flatten_subtitle_fetch(to_raw())
    return re.sub(r"\s+", " ", str(data)).strip()


class YouTubeParser(BaseParser):
    """YouTube: текст из субтитров и (опционально) snippet через Data API v3.

    * Субтитры: сначала ``ru``/``en``, иначе любая авто-расшифровка, иначе
      любой доступный вариант.
    * Data API: при ``YOUTUBE_API_KEY`` подмешиваются ``title`` и
      ``description``; при ошибке/отсутствии ключа — продолжаем без них.
    """

    source_type: str = "youtube"
    MAX_TEXT_LENGTH: int = 50_000

    def __init__(self, youtube_api_key: str = "") -> None:
        self._api_key: str = (youtube_api_key or "").strip()

    @staticmethod
    def can_parse(url: str) -> bool:
        """YouTube: watch, Shorts, youtu.be (m.youtube.com учитывается)."""
        if not isinstance(url, str) or not url.strip():
            return False
        u = url.lower()
        u = u.replace("m.youtube.com", "youtube.com")
        if "youtu.be/" in u:
            return True
        if "youtube.com/watch" in u:
            return True
        if "youtube.com/shorts/" in u:
            return True
        return False

    async def parse(self, url: str) -> str:
        if not self.can_parse(url):
            raise ValueError(f"YouTubeParser не поддерживает URL: {url!r}")
        # Тяжёлая логика — в потоке, чтобы не блокировать event loop.
        return await asyncio.to_thread(self._parse_sync, url)

    def _parse_sync(self, url: str) -> str:
        video_id = _extract_youtube_video_id(url)
        if not video_id or len(video_id) != 11:
            raise ValueError("Некорректный YouTube URL")
        logger.info("🎬 Обнаружено YouTube-видео: %s", video_id)

        sub_text, _sub_lang, _sub_failed = self._load_subtitles(video_id)

        title, description = self._load_snippet_or_empty(video_id, have_subs=bool(sub_text))

        if not sub_text and (title or description):
            logger.warning("⚠️ Субтитры не найдены, извлекаю только описание")
        if title or description:
            logger.info("📋 Получено описание через YouTube API")

        parts: List[str] = []
        if title:
            parts.append(title.strip())
        if description:
            parts.append(description.strip())
        if sub_text:
            parts.append(sub_text)

        if not any(p.strip() for p in parts):
            raise RuntimeError("Не удалось извлечь текст: субтитры и API недоступны")

        text = "\n\n".join(p for p in parts if p and p.strip())
        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]
        return text

    def _load_subtitles(self, video_id: str) -> Tuple[str, str, bool]:
        """Субтитры: (текст, язык, была_ли_тех_ошибка_при_загрузке)."""
        from youtube_transcript_api._errors import (  # type: ignore[import-not-found]
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
        )

        tlist: Any
        try:
            tlist = _youtube_list_transcripts(video_id)
        except VideoUnavailable as exc:
            raise RuntimeError("Видео не найдено") from exc
        except TranscriptsDisabled as exc:
            logger.warning("Субтитры отключены для видео: %s", exc)
            return "", "", False
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ Ошибка получения субтитров: %s", exc)
            # Фолбэк: прямой get_transcript (другой кодовый путь, иногда срабатывает
            # при сбоях list_transcripts / смене разметки YouTube).
            try:
                from youtube_transcript_api import YouTubeTranscriptApi

                ytt = YouTubeTranscriptApi
                try:
                    raw_fb = ytt.get_transcript(  # type: ignore[attr-defined]
                        video_id, languages=["ru", "en"]
                    )
                except Exception:  # noqa: BLE001
                    raw_fb = ytt.get_transcript(video_id)  # type: ignore[attr-defined]
            except Exception as exc2:  # noqa: BLE001
                logger.error("❌ Ошибка получения субтитров: %s", exc2)
                return "", "", True
            sub_fb = _flatten_subtitle_fetch(raw_fb)
            if sub_fb:
                logger.info(
                    "📝 Получены субтитры (фолбэк get_transcript): %s символов",
                    self._format_number(len(sub_fb)),
                )
            return sub_fb, "auto", False

        tr_obj: Any = None
        lang: str = ""
        try:
            tr_obj = tlist.find_transcript(["ru", "en"])
        except NoTranscriptFound:
            tr_obj = None

        if tr_obj is None:
            for t in tlist:  # type: ignore[operator]
                if getattr(t, "is_generated", False):
                    tr_obj = t
                    break
        if tr_obj is None:
            for t in tlist:  # type: ignore[operator]
                tr_obj = t
                break
        if tr_obj is None:
            return "", "", False

        lang = str(getattr(tr_obj, "language_code", None) or getattr(tr_obj, "language", None) or "?")
        try:
            raw = tr_obj.fetch()
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ Ошибка получения субтитров: %s", exc)
            return "", "", True
        sub_text = _flatten_subtitle_fetch(raw)
        if sub_text:
            logger.info(
                "📝 Получены субтитры (%s): %s символов",
                lang,
                self._format_number(len(sub_text)),
            )
        return sub_text, lang, False

    def _load_snippet_or_empty(self, video_id: str, have_subs: bool) -> Tuple[str, str]:
        """Заголовок и описание; при отсутствии ключа или ошибке — пустые строки."""
        if not self._api_key:
            return "", ""
        try:
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube Data API недоступен (import): %s", exc)
            return "", ""
        try:
            youtube = build("youtube", "v3", developerKey=self._api_key, cache_discovery=False)
            req = youtube.videos().list(part="snippet", id=video_id)
            resp: Dict[str, Any] = req.execute()
        except HttpError as exc:  # type: ignore[misc]
            code = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
            if code == 404 and not have_subs:
                raise RuntimeError("Видео не найдено") from exc
            if code == 404 and have_subs:
                logger.warning("YouTube Data API: 404, используем только субтитры")
            else:
                logger.warning(
                    "YouTube Data API: запрос не выполнен, продолжаем без описания: %s", exc
                )
            return "", ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube Data API: запрос не выполнен, продолжаем без описания: %s", exc)
            return "", ""

        items = resp.get("items") or []
        if not items:
            if have_subs:
                logger.warning("YouTube Data API: пустой ответ по id, остаёмся на субтитрах")
                return "", ""
            raise RuntimeError("Видео не найдено")
        sn: Dict[str, Any] = items[0].get("snippet") or {}
        return str(sn.get("title", "") or ""), str(sn.get("description", "") or "")

    @staticmethod
    def _format_number(value: int) -> str:
        return f"{value:,}".replace(",", " ")


# --------------------------------------------------------------------------- #
# Реестр парсеров
# --------------------------------------------------------------------------- #

class ParserRegistry:
    """Реестр парсеров: маршрутизирует URL к подходящему парсеру.

    Порядок регистрации важен: парсер, добавленный раньше,
    проверяется раньше. Поэтому специализированные парсеры
    (YouTube, Instagram, TikTok) должны регистрироваться **до**
    универсального `WebParser`.
    """

    def __init__(self) -> None:
        self._parsers: List[BaseParser] = []

    def register(self, parser: BaseParser) -> None:
        """Добавить парсер в реестр.

        Args:
            parser: Экземпляр наследника `BaseParser`.
        """
        self._parsers.append(parser)
        logger.info("📝 Зарегистрирован парсер: %s", parser.source_type)

    def get_parser(self, url: str) -> Optional[BaseParser]:
        """Вернуть первый парсер, умеющий обработать `url`, или None."""
        for parser in self._parsers:
            try:
                if parser.can_parse(url):
                    return parser
            except Exception as exc:  # noqa: BLE001 — защитная полоса
                logger.warning(
                    "⚠️  %s.can_parse упал на %r: %s",
                    type(parser).__name__, url, exc,
                )
        return None

    async def parse(self, url: str) -> str:
        """Найти подходящий парсер и делегировать ему работу.

        Raises:
            ValueError: если ни один из зарегистрированных парсеров
                не принял URL.
        """
        parser = self.get_parser(url)
        if parser is None:
            raise ValueError(f"Не удалось найти парсер для URL: {url}")

        logger.info("🔍 Парсинг через %s: %s", parser.source_type, url[:80])
        return await parser.parse(url)

    @property
    def parsers(self) -> List[BaseParser]:
        """Копия списка зарегистрированных парсеров (read-only снаружи)."""
        return list(self._parsers)


# --------------------------------------------------------------------------- #
# Фабрика
# --------------------------------------------------------------------------- #

def create_parser_registry(cfg: Optional[object] = None) -> ParserRegistry:
    """Построить реестр с набором парсеров по умолчанию.

    ``YouTubeParser`` регистрируется **до** ``WebParser``, чтобы
    YouTube-URL не уходили в обычный веб-парсер.

    Args:
        cfg: Объект с полем ``youtube_api_key`` (как :class:`config.Config`);
             если ``None`` — подставляется глобальный ``config`` из ``config.py``.

    Returns:
        Готовый к использованию `ParserRegistry`.
    """
    if cfg is None:
        from config import config as _cfg  # ленивый импорт, избегаем циклов
        cfg = _cfg
    api_key = str(getattr(cfg, "youtube_api_key", "") or "")
    registry = ParserRegistry()
    registry.register(YouTubeParser(youtube_api_key=api_key))
    registry.register(WebParser())
    return registry


# --------------------------------------------------------------------------- #
# Тестовый запуск
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    async def _test() -> None:
        # --- YouTube: can_parse по формам ссылок ---
        assert YouTubeParser.can_parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert YouTubeParser.can_parse("https://youtu.be/dQw4w9WgXcQ")
        assert YouTubeParser.can_parse("https://m.youtube.com/shorts/abcdefghijk1")
        assert not YouTubeParser.can_parse("https://eda.ru/recept/123")
        assert not YouTubeParser.can_parse("")
        assert _extract_youtube_video_id("https://www.youtube.com/watch?v=short") == ""
        print("✅ YouTube can_parse / extract ok")

        registry = create_parser_registry()

        # Порядок: YouTube раньше Web
        yt_p = registry.get_parser("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert yt_p is not None and getattr(yt_p, "source_type", None) == "youtube"
        assert registry.get_parser("https://eda.ru/test") is not None
        assert registry.get_parser("https://www.youtube.com/watch?v=abc") is not None
        assert registry.get_parser("not-a-url") is None
        assert registry.get_parser("ftp://example.com/file") is None
        print("✅ can_parse / реестр")

        # Пустой реестр
        empty = ParserRegistry()
        try:
            await empty.parse("https://eda.ru/")
        except ValueError as exc:
            print(f"✅ Пустой реестр: {exc}")
        else:
            raise AssertionError("Ожидался ValueError")

        # YouTube: только субтитры (принудительно без Data API)
        yurl = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

        class _CfgNoKey:
            youtube_api_key = ""

        reg_subs = create_parser_registry(_CfgNoKey())
        try:
            tyt = await reg_subs.parse(yurl)
            print(f"✅ YouTube (только субтитры): {len(tyt)} символов")
            if tyt:
                print(f"📝 YouTube, начало: {tyt[:200]!r}…")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  YouTube (субтитры) пропущен/ошибка: {exc}")

        # С YOUTUBE_API_KEY из .env/окружения (заголовок+описание+субтитры)
        import os

        if os.environ.get("YOUTUBE_API_KEY", "").strip():
            reg_full = create_parser_registry()
            try:
                t2 = await reg_full.parse(yurl)
                print(f"✅ YouTube + YOUTUBE_API_KEY: {len(t2)} символов")
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  YouTube + API: {exc}")
        else:
            print("ℹ️  YOUTUBE_API_KEY не задан — тест YouTube Data API пропущен")

        # Веб-страница
        test_url = "https://eda.ru/recepty/supy/klassicheskij-borshh-34567"
        try:
            text = await registry.parse(test_url)
            print(f"✅ Eda: извлечено {len(text)} символов")
            print(f"📝 Eda, первые 200: {text[:200]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Eda тест пропущен: {exc}")

    asyncio.run(_test())
