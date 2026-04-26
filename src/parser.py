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
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
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

# User-Agent для oEmbed / публичной страницы watch (без Data API).
_YT_PUBLIC_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _youtube_oembed_title(page_url: str) -> Tuple[str, Optional[str]]:
    """Заголовок через публичный oEmbed (без ключа API). (title, reason_if_empty)."""
    q = urllib.parse.urlencode({"url": page_url.strip(), "format": "json"})
    u = "https://www.youtube.com/oembed?" + q
    req = urllib.request.Request(u, headers={"User-Agent": _YT_PUBLIC_UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        title = str(data.get("title") or "").strip()
        return (title, None if title else "oEmbed: пустой title")
    except urllib.error.HTTPError as exc:
        reason = f"oEmbed HTTP {exc.code} (видео скрыто, удалено или embed отключён)"
        logger.warning("⚠️ %s", reason)
        return "", reason
    except Exception as exc:  # noqa: BLE001
        reason = f"oEmbed: {type(exc).__name__}: {exc}"
        logger.warning("⚠️ %s", reason)
        return "", reason


def _youtube_watch_page_description(video_id: str) -> Tuple[str, Optional[str]]:
    """Короткое описание из meta og:description / description на /watch (без API)."""
    watch = f"https://www.youtube.com/watch?v={video_id}"
    req = urllib.request.Request(
        watch,
        headers={
            "User-Agent": _YT_PUBLIC_UA,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        reason = f"страница watch: {type(exc).__name__}: {exc}"
        logger.warning("⚠️ Не удалось загрузить HTML: %s", reason)
        return "", reason
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:description")
    if og and og.get("content"):
        txt = str(og["content"]).strip()
        if txt:
            return txt, None
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        txt = str(meta["content"]).strip()
        if txt:
            return txt, None
    return "", "на странице нет og:description / meta description"


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
    """YouTube: субтитры, затем (опц.) Data API, затем oEmbed и HTML watch.

    Порядок: ru/en и автоген на любом языке → YouTube Data API (ключ)
    → oEmbed-заголовок → meta-описание со страницы. Без субтитров
    остаётся доступный текст (описание), без необработанных падений.
    """

    source_type: str = "youtube"
    MAX_TEXT_LENGTH: int = 50_000

    _RUNTIME_NO_TEXT = "Видео не содержит субтитров или описания"

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
        return await asyncio.to_thread(self._parse_sync, url)

    def _parse_sync(self, url: str) -> str:
        video_id = _extract_youtube_video_id(url)
        if not video_id or len(video_id) != 11:
            raise ValueError("Некорректный YouTube URL")
        page_url = url.strip()
        logger.info("🎬 Обнаружено YouTube-видео: %s", video_id)

        sub_text, _sub_lang, sub_reason = self._load_subtitles(video_id)
        if not sub_text and sub_reason:
            logger.warning(
                "⚠️ Субтитры не получены (%s). Пробуем описание и публичные источники.",
                sub_reason,
            )

        title, desc = self._load_snippet_data_api(video_id, have_subs=bool(sub_text))
        if (title and title.strip()) or (desc and desc.strip()):
            logger.info("📋 Получено описание через YouTube Data API")

        # Публичные fallback: oEmbed (заголовок), стр. watch (og:description)
        oembed_title, _o_msg = _youtube_oembed_title(page_url) if not (title and title.strip()) else ("", None)
        if oembed_title:
            logger.info("📋 Заголовок получен через oEmbed (без Data API)")

        page_desc, page_reason = ("", None)
        if not (desc and desc.strip()):
            page_desc, page_reason = _youtube_watch_page_description(video_id)
            if page_desc:
                logger.info("📋 Описание/фрагмент взяты с публичной страницы (meta og:description)")
            elif page_reason:
                logger.warning("⚠️ Публичная страница: %s", page_reason)

        final_title = (title or oembed_title or "").strip()
        final_desc = (desc or page_desc or "").strip()

        if (final_title or final_desc) and not sub_text:
            logger.warning(
                "⚠️ Субтитры отсутствуют; используем заголовок и/или описание (см. причины выше)"
            )

        parts: List[str] = []
        if final_title:
            parts.append(final_title)
        if final_desc:
            parts.append(final_desc)
        if sub_text:
            parts.append(sub_text)

        if not any(p.strip() for p in parts):
            logger.error("❌ %s (субтитры, Data API, oEmbed, meta-описание — всё пусто)", self._RUNTIME_NO_TEXT)
            raise RuntimeError(self._RUNTIME_NO_TEXT)

        text = "\n\n".join(p for p in parts if p and p.strip())
        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]
        return text

    def _load_subtitles(self, video_id: str) -> Tuple[str, str, str]:
        """Субтитры: (текст, язык, причина_пусто — для логов; пусто = «OK»)."""
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (  # type: ignore[import-not-found]
            NoTranscriptFound,
            TranscriptsDisabled,
            VideoUnavailable,
        )

        tlist: Any
        try:
            tlist = _youtube_list_transcripts(video_id)
        except VideoUnavailable as exc:
            msg = f"ролик недоступен для субтитров (VideoUnavailable: {exc})"
            logger.warning("⚠️ %s", msg)
            return self._fallback_get_transcript_only(video_id, YouTubeTranscriptApi, reason_prefix=msg)
        except TranscriptsDisabled as exc:
            reason = f"субтитры отключены автором: {exc}"
            logger.warning("⚠️ %s", reason)
            return "", "", reason
        except Exception as exc:  # noqa: BLE001
            err_t = f"{type(exc).__name__}: {exc}"
            logger.error("❌ list_transcripts: %s", err_t)
            return self._fallback_get_transcript_only(
                video_id, YouTubeTranscriptApi, reason_prefix=err_t
            )

        all_tr: List[Any] = []
        try:
            for t in tlist:  # type: ignore[operator]
                all_tr.append(t)
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ обход списка дорожек: %s", exc)
            all_tr = []

        if not all_tr:
            reason = "YouTube не вернул ни одной дорожки субтитров (пустой список)"
            logger.warning("⚠️ %s", reason)
            return "", "", reason

        tr_obj: Any = None
        pick_reason = ""

        try:
            tr_obj = tlist.find_transcript(["ru", "en"])
            pick_reason = "дорожка ru/en (ручная/автоген)"
        except NoTranscriptFound:
            logger.info("ℹ️ субтитры ru/en не найдены (NoTranscriptFound) — ищем автоген/другой язык")
            tr_obj = None
            for t in all_tr:
                if getattr(t, "is_generated", False):
                    tr_obj = t
                    pick_reason = f"автоген, язык {getattr(t, 'language_code', '?')}"
                    break
            if tr_obj is None and all_tr:
                tr_obj = all_tr[0]
                pick_reason = f"доступная дорожка, язык {getattr(tr_obj, 'language_code', '?')}"

        lang = str(
            getattr(tr_obj, "language_code", None) or getattr(tr_obj, "language", None) or "?"
        )
        try:
            raw = tr_obj.fetch()
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ fetch() субтитров: %s: %s", type(exc).__name__, exc)
            return "", "", f"сбой fetch: {type(exc).__name__}"

        sub_text = _flatten_subtitle_fetch(raw)
        if not sub_text:
            r = f"текст дорожки пуст ({pick_reason})"
            logger.warning("⚠️ %s", r)
            return "", "", r

        logger.info("📝 Получены субтитры (%s): %s символов — %s", lang, self._format_number(len(sub_text)), pick_reason)
        return sub_text, lang, ""

    def _fallback_get_transcript_only(
        self, video_id: str, ytt: Any, reason_prefix: str
    ) -> Tuple[str, str, str]:
        """Фолбэк get_transcript, если list_transcripts не сработал (другой путь)."""
        try:
            try:
                raw = ytt.get_transcript(video_id, languages=["ru", "en"])  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                raw = ytt.get_transcript(video_id)  # type: ignore[attr-defined]
        except Exception as exc2:  # noqa: BLE001
            r = f"{reason_prefix}; get_transcript: {type(exc2).__name__}: {exc2}"
            logger.error("❌ %s", r)
            return "", "", r
        sub = _flatten_subtitle_fetch(raw)
        if sub:
            logger.info("📝 Получены субтитры (get_transcript): %s символов", self._format_number(len(sub)))
            return sub, "mixed", ""
        return "", "", f"{reason_prefix}; get_transcript вернул пустой текст"

    def _load_snippet_data_api(self, video_id: str, have_subs: bool) -> Tuple[str, str]:
        """Заголовок и описание через Data API; никаких raise — пусто при ошибке."""
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
            logger.warning(
                "YouTube Data API: HTTP %s — продолжаем без snippet (oEmbed/HTML): %s",
                code,
                exc,
            )
            return "", ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("YouTube Data API: запрос не выполнен, продолжаем без описания: %s", exc)
            return "", ""

        items = resp.get("items") or []
        if not items:
            if have_subs:
                logger.warning("YouTube Data API: пустой items — оставляем только субтитры")
            else:
                logger.warning("YouTube Data API: пустой items (ид не найден или нет прав) — oEmbed / meta")
            return "", ""
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

        # Shorts / видео без субтитров: только oEmbed + meta (без API ключа)
        short_url = "https://www.youtube.com/shorts/fdqQJNXSDbI"
        try:
            tshort = await reg_subs.parse(short_url)
            print(f"✅ Shorts (без Data API): {len(tshort)} символов")
        except RuntimeError as exc:
            print(f"ℹ️  Shorts: нет текста — {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Shorts: {exc}")

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
