"""
Модуль парсеров рецептов Remy Bot.

* `BaseParser` — абстрактный контракт (`can_parse`, `parse`, `source_type`).
* `WebParser` — обычные HTTP(S)-страницы с рецептами.
* `YouTubeParser` — заголовок и описание через YouTube Data API v3; субтитры — `youtube-transcript-api`
  (опционально через HTTP(S) прокси из конфига с ротацией при 429).
* `ParserRegistry` / `create_parser_registry` — маршрутизация и фабрика.
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import List, Optional

import aiohttp
from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    IpBlocked,
    NoTranscriptFound,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeRequestFailed,
)

try:
    from readability import Document as _ReadabilityDocument
except Exception:  # noqa: BLE001
    _ReadabilityDocument = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Абстрактный базовый класс
# --------------------------------------------------------------------------- #


class BaseParser(ABC):
    """Абстрактный базовый класс для всех парсеров рецептов."""

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Тип источника (``website``, ``youtube`` и т. д.)."""

    @staticmethod
    @abstractmethod
    def can_parse(url: str) -> bool:
        """Быстрая проверка URL (без сети)."""

    @abstractmethod
    async def parse(self, url: str) -> str:
        """Извлечь сырой текст рецепта."""


# --------------------------------------------------------------------------- #
# Реализация: обычные веб-страницы
# --------------------------------------------------------------------------- #


class WebParser(BaseParser):
    """Парсер веб-страниц: aiohttp + readability + BeautifulSoup."""

    source_type: str = "website"
    MAX_TEXT_LENGTH: int = 50_000
    TIMEOUT_SECONDS: float = 30.0

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

    _TAGS_TO_REMOVE: tuple = (
        "script", "style", "nav", "footer", "header",
        "aside", "form", "iframe", "noscript", "svg",
    )

    @staticmethod
    def can_parse(url: str) -> bool:
        if not isinstance(url, str):
            return False
        return url.startswith(("http://", "https://"))

    async def parse(self, url: str) -> str:
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

    async def _fetch(self, url: str) -> str:
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)

        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=self.HEADERS) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status >= 400:
                        logger.error("❌ Ошибка парсинга %s: HTTP %s", url, response.status)
                        raise RuntimeError(f"Ошибка HTTP {response.status} при загрузке страницы")
                    html = await response.text(errors="replace")
                    size_kb = max(len(html) // 1024, 1)
                    logger.info("✅ Страница загружена (%s), размер: %sKB", response.status, size_kb)
                    return html

        except asyncio.TimeoutError:
            logger.error("❌ Ошибка парсинга %s: таймаут соединения", url)
            raise RuntimeError("Таймаут при загрузке страницы") from None
        except aiohttp.ClientError as exc:
            logger.error("❌ Ошибка парсинга %s: %s", url, exc)
            raise RuntimeError(f"Сетевая ошибка: {exc}") from exc

    def _extract_text(self, html: str) -> str:
        content_html = html
        if _ReadabilityDocument is not None:
            try:
                doc = _ReadabilityDocument(html)
                summary = doc.summary(html_partial=True)
                if summary and summary.strip():
                    content_html = summary
            except Exception as exc:  # noqa: BLE001
                logger.warning("⚠️  readability не справился, используем полный HTML: %s", exc)

        soup = BeautifulSoup(content_html, "lxml")
        for tag in soup(self._TAGS_TO_REMOVE):
            tag.decompose()
        raw_text = soup.get_text(separator="\n")
        return self._clean_text(raw_text)

    @staticmethod
    def _clean_text(text: str) -> str:
        lines = text.splitlines()
        cleaned_lines: List[str] = []
        for line in lines:
            collapsed = re.sub(r"[ \t\u00a0]+", " ", line).strip()
            if collapsed:
                cleaned_lines.append(collapsed)
        return "\n".join(cleaned_lines)

    @staticmethod
    def _format_number(value: int) -> str:
        return f"{value:,}".replace(",", " ")


# --------------------------------------------------------------------------- #
# YouTube: очистка текста из Data API
# --------------------------------------------------------------------------- #


_HTML_TAG_RE = re.compile(r"<[^>]+>", re.IGNORECASE)

# Хэштеги: не трогать # внутри «числа #1» — требуется слово/кириллица после #.
_YT_DESC_HASHTAG_RE = re.compile(
    r"(?<!\w)#[A-Za-z0-9_\u0400-\u04FF\u0500-\u052F]+"
)

# Эмодзи (не затрагивают U+00B0 °, U+00B5 µ).
_YT_DESC_EMOJI_RE = re.compile(
    "["
    "\U0001F1E0-\U0001F1FF"
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\uFE00-\uFE0F"
    "\u200D"
    "]+"
)


def _strip_youtube_description_text(text: str) -> str:
    """Описание: без эмодзи и хэштегов; не трогать °C, µg и т. п."""
    s = (text or "").replace("\r\n", "\n")
    s = _YT_DESC_HASHTAG_RE.sub(" ", s)
    s = _YT_DESC_EMOJI_RE.sub("", s)
    lines_out: List[str] = []
    for line in s.splitlines():
        collapsed = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if collapsed:
            lines_out.append(collapsed)
    s = "\n".join(lines_out)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    return s.strip()


def _strip_youtube_title_text(text: str) -> str:
    """Заголовок: HTML-теги в тексте, эмодзи, лишние пробелы."""
    s = (text or "").strip()
    s = _HTML_TAG_RE.sub(" ", s)
    s = _YT_DESC_EMOJI_RE.sub("", s)
    s = re.sub(r"[ \t\u00a0]+", " ", s).strip()
    return s


def _normalize_youtube_proxy_url(proxy_url: str) -> str:
    """Приводит URL прокси к виду, который ожидает requests (как в документации библиотеки).

    Поддерживается ``http://user:pass@host:port`` и ``https://...``. Если схема не указана,
    добавляется ``http://`` (резидентские прокси вроде Webshare обычно идут по HTTP).
    """
    u = (proxy_url or "").strip()
    if not u:
        return u
    if "://" not in u:
        u = f"http://{u}"
    return u


def _youtube_transcript_api_client(proxy_url: str | None) -> YouTubeTranscriptApi:
    """Клиент youtube-transcript-api: без прокси или с ``requests.Session.proxies``.

    Явная настройка Session эквивалентна ``GenericProxyConfig.to_requests_dict()`` из библиотеки
    и стабильно работает с ``http://user:pass@host:port``. Заголовок ``Connection: close`` снижает
    залипание на одном IP при ротируемых резидентских прокси.
    """
    if not (proxy_url or "").strip():
        return YouTubeTranscriptApi()
    from requests import Session

    u = _normalize_youtube_proxy_url(proxy_url)
    session = Session()
    session.proxies = {"http": u, "https": u}
    session.headers.update(
        {
            "Accept-Language": "en-US",
            "Connection": "close",
        }
    )
    return YouTubeTranscriptApi(http_client=session)


def _is_subtitles_unavailable(exc: BaseException) -> bool:
    return isinstance(exc, (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable))


def _is_youtube_transcript_rate_limit(exc: BaseException) -> bool:
    """Ошибки блокировки / 429 — переключение прокси или немедленная ошибка без прокси."""
    if isinstance(exc, (RequestBlocked, IpBlocked)):
        return True
    if isinstance(exc, YouTubeRequestFailed):
        r = str(getattr(exc, "reason", exc))
        low = r.lower()
        return "429" in r or "too many requests" in low
    s = f"{type(exc).__name__}: {exc}"
    low = s.lower()
    return "429" in s or "too many requests" in low


class YouTubeParser(BaseParser):
    """YouTube: Data API v3 — заголовок и описание; субтитры — transcript-api (опционально через прокси)."""

    source_type: str = "youtube"
    MAX_TEXT_LENGTH: int = 50_000

    def __init__(
        self,
        youtube_api_key: str = "",
        proxy_urls: list[str] | None = None,
    ) -> None:
        self.youtube_api_key = (youtube_api_key or "").strip()
        self._proxy_urls: list[str] = list(proxy_urls) if proxy_urls else []

    @staticmethod
    def _extract_youtube_video_id(url: str) -> str:
        """11-символьный id (watch, Shorts, youtu.be)."""
        s = (url or "").strip()
        m = re.search(
            r"(?:[?&]v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})\b",
            s,
            re.IGNORECASE,
        )
        return m.group(1) if m else ""

    @staticmethod
    def can_parse(url: str) -> bool:
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
        return await asyncio.to_thread(self._parse_sync, url)

    def _fetch_subtitles_via_api(self, video_id: str, proxy_url: str | None) -> str:
        api = _youtube_transcript_api_client(proxy_url)
        ft = api.fetch(video_id, languages=("ru", "en"))
        parts: List[str] = []
        for snippet in ft.snippets:
            t = (snippet.text or "").replace("\n", " ").strip()
            if t:
                parts.append(t)
        text = " ".join(parts)
        return re.sub(r"\s+", " ", text).strip()

    def _load_subtitles_no_proxy(self, video_id: str) -> str:
        try:
            return self._fetch_subtitles_via_api(video_id, None)
        except Exception as exc:  # noqa: BLE001
            if _is_subtitles_unavailable(exc):
                return ""
            if _is_youtube_transcript_rate_limit(exc):
                raise RuntimeError(
                    "YouTube временно ограничил доступ. Попробуйте позже."
                ) from exc
            logger.warning("Субтитры без прокси не получены: %s", exc)
            return ""

    def _load_subtitles_with_proxies(self, video_id: str, proxies: list[str]) -> str:
        for idx, proxy in enumerate(proxies):
            try:
                text = self._fetch_subtitles_via_api(video_id, proxy)
                if text:
                    logger.info(
                        "Получены субтитры через прокси %s (%s символов)",
                        proxy,
                        len(text),
                    )
                return text
            except Exception as exc:  # noqa: BLE001
                if _is_subtitles_unavailable(exc):
                    return ""
                if _is_youtube_transcript_rate_limit(exc):
                    if idx < len(proxies) - 1:
                        logger.warning(
                            "Прокси %s недоступен (429), переключаю на следующий",
                            proxy,
                        )
                        continue
                    logger.error("Все прокси исчерпаны, субтитры не получены")
                    raise RuntimeError(
                        "YouTube временно ограничил доступ. Попробуйте позже."
                    ) from exc
                if idx < len(proxies) - 1:
                    logger.warning(
                        "Прокси %s: субтитры не получены (%s), пробую следующий",
                        proxy,
                        type(exc).__name__,
                    )
                    continue
                logger.warning("Субтитры не получены после перебора прокси: %s", exc)
                return ""
        return ""

    def _load_subtitles_best_effort(self, video_id: str) -> str:
        if not self._proxy_urls:
            return self._load_subtitles_no_proxy(video_id)
        return self._load_subtitles_with_proxies(video_id, self._proxy_urls)

    def _parse_sync(self, url: str) -> str:
        video_id = self._extract_youtube_video_id(url)
        if not video_id or len(video_id) != 11:
            raise ValueError("Некорректный YouTube URL")

        logger.info("🎬 Обнаружено YouTube-видео: %s", video_id)

        api_key = self.youtube_api_key
        if not api_key:
            logger.warning("YouTube API ключ не задан, парсинг невозможен")
            raise RuntimeError(
                "YouTube API ключ не настроен. Добавьте YOUTUBE_API_KEY в переменные окружения."
            )

        try:
            youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
            response = (
                youtube.videos()
                .list(part="snippet,contentDetails", id=video_id)
                .execute()
            )
        except HttpError as exc:
            err = str(exc)
            logger.error("Ошибка YouTube Data API: %s", err)
            raise RuntimeError(err) from exc
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            logger.error("Ошибка YouTube Data API: %s", err)
            raise RuntimeError(err) from exc

        items = response.get("items") or []
        if not items:
            raise RuntimeError("Видео не найдено")

        snippet = items[0].get("snippet") or {}
        raw_title = snippet.get("title") or ""
        if not isinstance(raw_title, str):
            raw_title = str(raw_title)
        raw_desc = snippet.get("description") or ""
        if not isinstance(raw_desc, str):
            raw_desc = str(raw_desc) if raw_desc is not None else ""

        clean_title = _strip_youtube_title_text(raw_title)
        clean_desc = _strip_youtube_description_text(raw_desc) if raw_desc else ""

        title_preview = raw_title[:50] + ("..." if len(raw_title) > 50 else "")
        logger.info(
            'Получены данные через YouTube Data API: "%s"',
            title_preview,
        )

        sub_text = self._load_subtitles_best_effort(video_id)

        parts_out: List[str] = []
        if clean_title:
            parts_out.append(clean_title)
        if clean_desc:
            parts_out.append(clean_desc)
        core = "\n\n".join(parts_out)
        if sub_text:
            text = f"{core}\n\n{sub_text}" if core else sub_text
        else:
            text = core

        if not text.strip():
            logger.error("❌ Не удалось извлечь текст из видео (пустой заголовок и описание)")
            raise RuntimeError("Не удалось извлечь текст из видео")

        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]
        return text


# --------------------------------------------------------------------------- #
# Реестр
# --------------------------------------------------------------------------- #


class ParserRegistry:
    """Реестр: первый подходящий `can_parse` выигрывает."""

    def __init__(self) -> None:
        self._parsers: List[BaseParser] = []

    def register(self, parser: BaseParser) -> None:
        self._parsers.append(parser)
        logger.info("📝 Зарегистрирован парсер: %s", parser.source_type)

    def get_parser(self, url: str) -> Optional[BaseParser]:
        for parser in self._parsers:
            try:
                if parser.can_parse(url):
                    return parser
            except Exception as exc:  # noqa: BLE001
                logger.warning("⚠️  %s.can_parse упал на %r: %s", type(parser).__name__, url, exc)
        return None

    async def parse(self, url: str) -> str:
        parser = self.get_parser(url)
        if parser is None:
            raise ValueError(f"Не удалось найти парсер для URL: {url}")
        logger.info("🔍 Парсинг через %s: %s", parser.source_type, url[:80])
        return await parser.parse(url)

    @property
    def parsers(self) -> List[BaseParser]:
        return list(self._parsers)


def create_parser_registry(cfg: Optional[object] = None) -> ParserRegistry:
    """Реестр с YouTube (до Web). Передаются ``youtube_api_key`` и список прокси для субтитров."""
    if cfg is None:
        from config import config as _cfg
        cfg = _cfg
    api_key = str(getattr(cfg, "youtube_api_key", "") or "")
    raw_proxies = str(getattr(cfg, "youtube_proxy_url", "") or "")
    proxy_list = [p.strip() for p in raw_proxies.split(",") if p.strip()]
    registry = ParserRegistry()
    registry.register(YouTubeParser(youtube_api_key=api_key, proxy_urls=proxy_list))
    registry.register(WebParser())
    return registry


# --------------------------------------------------------------------------- #
# __main__
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys
    from unittest.mock import MagicMock, patch

    async def _test() -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        assert YouTubeParser.can_parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert YouTubeParser.can_parse("https://youtu.be/dQw4w9WgXcQ")
        assert YouTubeParser.can_parse("https://m.youtube.com/shorts/abcdefghijk")
        assert not YouTubeParser.can_parse("https://eda.ru/recept/123")
        assert not YouTubeParser.can_parse("")
        assert YouTubeParser._extract_youtube_video_id("https://www.youtube.com/watch?v=short") == ""
        assert _normalize_youtube_proxy_url("http://user:pass@host:8080") == "http://user:pass@host:8080"
        assert _normalize_youtube_proxy_url("user:pass@residential.host:80") == "http://user:pass@residential.host:80"
        assert _normalize_youtube_proxy_url("  https://u:p@proxy.example:443  ") == "https://u:p@proxy.example:443"
        _cli = _youtube_transcript_api_client("http://user:pass@127.0.0.1:8888")
        assert _cli is not None
        print("✅ YouTube can_parse / extract / прокси-URL")

        no_key = YouTubeParser(youtube_api_key="")
        try:
            await no_key.parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        except RuntimeError as exc:
            if "YOUTUBE_API_KEY" not in str(exc):
                raise AssertionError(f"Ожидалось сообщение про ключ: {exc}") from exc
            print("✅ Без API-ключа: понятный RuntimeError")
        else:
            raise AssertionError("Ожидался RuntimeError без YOUTUBE_API_KEY")

        this_mod = sys.modules[__name__]
        with patch.object(this_mod, "build") as mock_build, patch.object(
            YouTubeParser,
            "_fetch_subtitles_via_api",
            return_value="Mock subtitle line",
        ):
            mock_yt = MagicMock()
            mock_build.return_value = mock_yt
            mock_req = MagicMock()
            mock_yt.videos.return_value.list.return_value = mock_req
            mock_req.execute.return_value = {
                "items": [
                    {
                        "snippet": {
                            "title": "Mock Recipe Video Title Here",
                            "description": "Line one\n\nIngredients: test",
                        },
                        "contentDetails": {"duration": "PT5M30S"},
                    }
                ]
            }
            parsed = await YouTubeParser(youtube_api_key="test-key").parse(
                "https://www.youtube.com/watch?v=abcdefghijk"
            )
            assert "Mock Recipe" in parsed
            assert "Ingredients" in parsed
            assert "Mock subtitle" in parsed
            print("✅ parse() с моком Data API и субтитрами")

        from youtube_transcript_api._errors import IpBlocked

        with patch.object(this_mod, "build") as mock_build, patch.object(
            YouTubeParser,
            "_fetch_subtitles_via_api",
            side_effect=[
                IpBlocked("abcdefghijk"),
                "Text after proxy rotation",
            ],
        ):
            mock_yt = MagicMock()
            mock_build.return_value = mock_yt
            mock_req = MagicMock()
            mock_yt.videos.return_value.list.return_value = mock_req
            mock_req.execute.return_value = {
                "items": [
                    {
                        "snippet": {"title": "T", "description": "D"},
                        "contentDetails": {},
                    }
                ]
            }
            rotated = await YouTubeParser(
                youtube_api_key="test-key",
                proxy_urls=["http://proxy-a.example", "http://proxy-b.example"],
            ).parse("https://www.youtube.com/watch?v=abcdefghijk")
            assert "after proxy rotation" in rotated
            print("✅ ротация прокси при блокировке (мок)")

        import os

        _live_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        _live_proxy = os.getenv("YOUTUBE_PROXY_URL", "").strip()
        if _live_key:
            try:
                live_proxies = [p.strip() for p in _live_proxy.split(",") if p.strip()]
                live_text = await YouTubeParser(
                    youtube_api_key=_live_key,
                    proxy_urls=live_proxies,
                ).parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
                print(
                    f"✅ YouTube live (API"
                    f"{' + прокси' if live_proxies else ''}): {len(live_text)} символов"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  YouTube live: {exc}")

        registry = create_parser_registry()
        yt_p = registry.get_parser("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert yt_p is not None and getattr(yt_p, "source_type", None) == "youtube"
        assert registry.get_parser("https://eda.ru/test") is not None
        assert registry.get_parser("not-a-url") is None
        print("✅ can_parse / реестр")

        empty = ParserRegistry()
        try:
            await empty.parse("https://eda.ru/")
        except ValueError as exc:
            print(f"✅ Пустой реестр: {exc}")
        else:
            raise AssertionError("Ожидался ValueError")

        try:
            text = await registry.parse("https://eda.ru/recepty/supy/klassicheskij-borshh-34567")
            print(f"✅ Eda: {len(text)} символов")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Eda: {exc}")

    import asyncio as _asyncio

    _asyncio.run(_test())
