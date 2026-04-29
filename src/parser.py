"""
Модуль парсеров рецептов Remy Bot.

* `BaseParser` — абстрактный контракт (`can_parse`, `parse`, `source_type`).
* `WebParser` — обычные HTTP(S)-страницы с рецептами.
* `YouTubeParser` — YouTube: `yt-dlp` для title/description/tags +
  `youtube-transcript-api` для текста субтитров (приоритет ASR → ручные).
  При запросе «Sign in…» можно передать Netscape cookies — только для yt-dlp
  (метаданные); текст субтитров — через ``youtube_transcript_api``, без этого файла.
* `ParserRegistry` / `create_parser_registry` — маршрутизация и фабрика.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp
from bs4 import BeautifulSoup

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
        lines: Iterable[str] = text.splitlines()
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
# YouTube: вспомогательные функции
# --------------------------------------------------------------------------- #


def _extract_youtube_video_id(url: str) -> str:
    """11-символьный id (watch, Shorts, youtu.be)."""
    s = (url or "").strip()
    m = re.search(
        r"(?:[?&]v=|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})\b",
        s,
        re.IGNORECASE,
    )
    return m.group(1) if m else ""


def _youtube_list_transcripts(video_id: str, cookies: Optional[str] = None) -> Any:
    """Совместимость youtube-transcript-api 0.6.x (list_transcripts) и 1.x (list).

    ``cookies`` — путь к Netscape cookies.txt (тот же файл, что для yt-dlp), опционально.
    """
    from youtube_transcript_api import YouTubeTranscriptApi

    if hasattr(YouTubeTranscriptApi, "list_transcripts"):
        return YouTubeTranscriptApi.list_transcripts(  # type: ignore[no-any-return, misc]
            video_id,
            proxies=None,
            cookies=cookies,
        )
    ytt = YouTubeTranscriptApi()  # type: ignore[call-arg]
    return ytt.list(video_id, proxies=None, cookies=cookies)


def _exc_is_transcript_rate_limit(exc: BaseException) -> bool:
    """429 / Too Many Requests от transcript-api — имеет смысл повторить запрос позже."""
    try:
        from youtube_transcript_api._errors import TooManyRequests, YouTubeRequestFailed
    except ImportError:
        return "429" in f"{exc}" and "Too Many" in f"{exc}"

    if isinstance(exc, TooManyRequests):
        return True
    if isinstance(exc, YouTubeRequestFailed):
        r = getattr(exc, "reason", str(exc))
        rs = str(r)
        return "429" in rs or "Too Many Requests" in rs
    text = f"{type(exc).__name__}: {exc}"
    return "429" in text and "Too Many Requests" in text


def _youtube_watch_html_looks_rate_limited(html_text: str) -> bool:
    """Эвристика страницы-заглушки после лимита IP (не полноценный watch с og:-мета)."""
    if not html_text.strip():
        return False
    low = html_text.lower()
    if "429" in html_text:
        return True
    if "too many requests" in low:
        return True
    # Частые формулировки заглушки / блокировки (не используем голый «sorry» — ложные срабатывания).
    if "sorry" in low and any(
        x in low for x in ("try again", "something went wrong", "many requests", "unusual traffic")
    ):
        return True
    return False


_SUBTITLE_HTML_TAG_RE = re.compile(r"<[^>]+>", re.IGNORECASE)
_CC_BRACKET_NOISE = re.compile(
    r"\[[^\]]{0,200}?"
    r"(?:"
    r"музык|аплодисмент|звук|шум|тих|тиш|инструмент|"
    r"music|applause|laughter|laughing|silence|singing|noise|indistinct|"
    r"inaudible|crowd|beat|beep|click|bass|guitar|piano|drum|"
    r"♪|♫|\u266a|\u266b"
    r")"
    r"[^\]]{0,200}?\]",
    re.IGNORECASE,
)
_CC_TIME_BRACKETS = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?(?:\s*-\s*\d{1,2}:\d{2}(?::\d{2})?)?\]")


# Хэштеги: не трогать # внутри «числа #1» (редко) — требуется слово/кириллица после #.
_YT_DESC_HASHTAG_RE = re.compile(
    r"(?<!\w)#[A-Za-z0-9_\u0400-\u04FF\u0500-\u052F]+"
)

# Эмодзи (не затрагивают U+00B0 °, U+00B5 µ, латиницу/кириллицу).
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


def _raw_subtitle_texts(data: Any) -> List[str]:
    if not data:
        return []
    if isinstance(data, list):
        return [
            str(item.get("text", "") or "")
            for item in data
            if isinstance(item, dict) and "text" in item
        ]
    to_raw = getattr(data, "to_raw_data", None)
    if callable(to_raw):
        return _raw_subtitle_texts(to_raw())
    return []


def _clean_one_subtitle_line(text: str) -> str:
    t = (text or "").replace("\n", " ").replace("\r", " ")
    t = _SUBTITLE_HTML_TAG_RE.sub(" ", t)
    t = _CC_BRACKET_NOISE.sub(" ", t)
    t = _CC_TIME_BRACKETS.sub(" ", t)
    t = re.sub(r"[ \t\u00a0]+", " ", t).strip()
    return t


def _join_subtitle_segments(segments: List[str]) -> str:
    parts = [_clean_one_subtitle_line(s) for s in segments if s and str(s).strip()]
    text = " ".join(parts)
    return re.sub(r"\s+", " ", text).strip()


def _clean_subtitle_fetch(data: Any) -> str:
    if not data:
        return ""
    if isinstance(data, str):
        return _join_subtitle_segments([data])
    parts = _raw_subtitle_texts(data)
    if not parts:
        to_raw = getattr(data, "to_raw_data", None)
        if not callable(to_raw):
            return re.sub(r"\s+", " ", str(data)).strip()
        return _clean_subtitle_fetch(to_raw())  # type: ignore[no-untyped-call]
    return _join_subtitle_segments(parts)


def _transcript_lang_code(tr_obj: Any) -> str:
    return str(
        getattr(tr_obj, "language_code", None) or getattr(tr_obj, "language", None) or "?"
    )


def _select_transcript_track(
    tlist: Any, all_tr: List[Any], NoTranscriptFound: Any
) -> Tuple[Any, str, str]:
    """ASR ru → ASR en → ручные ru/en → find_transcript → любая ASR → первая дорожка."""
    if hasattr(tlist, "find_generated_transcript"):
        try:
            t = tlist.find_generated_transcript(["ru", "en"])
            return t, "auto", "авто поиск ru/en"
        except NoTranscriptFound:
            pass

    for code in ("ru", "en"):
        for t in all_tr:
            if getattr(t, "is_generated", False) and getattr(t, "language_code", None) == code:
                return t, "auto", f"asr {code} (обход по списку)"

    if hasattr(tlist, "find_manually_created_transcript"):
        try:
            t = tlist.find_manually_created_transcript(["ru", "en"])
            return t, "manual", "ручные ru/en"
        except NoTranscriptFound:
            pass
    for code in ("ru", "en"):
        for t in all_tr:
            if (
                getattr(t, "language_code", None) == code
                and not getattr(t, "is_generated", False)
            ):
                return t, "manual", f"ручной {code} (обход по списку)"

    try:
        t = tlist.find_transcript(["ru", "en"])
        kind = "auto" if getattr(t, "is_generated", False) else "manual"
        return t, kind, "find_transcript ru/en"
    except NoTranscriptFound:
        pass

    for t in all_tr:
        if getattr(t, "is_generated", False):
            return t, "auto", "первая доступная ASR"
    if all_tr:
        t = all_tr[0]
        kind = "auto" if getattr(t, "is_generated", False) else "manual"
        return t, kind, "первая доступная дорожка"

    raise RuntimeError("_select_transcript_track: all_tr is empty (внутренняя ошибка)")


def _strip_youtube_description_text(text: str) -> str:
    """Описание/крупные поля: без эмодзи и хэштегов; не трогать °C, µg и т. п."""
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
    """Заголовок: HTML, эмодзи, лишние пробелы (без съедания буквенного текста)."""
    s = (text or "").strip()
    s = _SUBTITLE_HTML_TAG_RE.sub(" ", s)
    s = _YT_DESC_EMOJI_RE.sub("", s)
    s = re.sub(r"[ \t\u00a0]+", " ", s).strip()
    return s


def _format_tags_line(tags: List[Any], max_len: int = 4_000) -> str:
    if not tags:
        return ""
    out: List[str] = []
    for t in tags:
        if not isinstance(t, str):
            t = str(t) if t is not None else ""
        t = t.strip()
        if not t:
            continue
        t = _YT_DESC_EMOJI_RE.sub("", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            out.append(t)
    s = ", ".join(out)
    if len(s) > max_len:
        s = s[:max_len] + "…"
    return s


def _yt_dlp_extract_metadata(url: str, cookie_file: Optional[str] = None) -> Dict[str, Any]:
    """Title, description, tags + списки дорожек субтитров (URL-ы) без скачивания видео.

    **Область cookies:** параметр ``cookie_file`` используется **только здесь**
    (опции yt-dlp для ``extract_info``): заголовок, описание, теги, метаданные дорожек.

    Текст субтитров для карточки рецепта берётся отдельным кодом —
    :meth:`YouTubeParser._load_subtitles` через ``youtube_transcript_api``, который
    **не получает** этот файл cookies (другой HTTP-клиент и контракт библиотеки).
    Если в будущем понадобится обход капчи и для дорожек transcript-api, это потребует
    отдельной интеграции в эту библиотеку или резервного пути через yt-dlp.

    `writesubtitles` / `writeautomaticsub` с `skip_download` заполняют в ``info``
    поля ``subtitles`` / ``automatic_captions`` без записи файлов на диск.

    Args:
        cookie_file: Путь к Netscape cookies.txt — только если файл уже проверен
            на существование (см. :meth:`YouTubeParser._resolved_cookie_file`).
    """
    import yt_dlp

    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["ru", "en"],
        "skip_download": True,
        "extract_flat": False,
        "noplaylist": True,
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    with yt_dlp.YoutubeDL(opts) as ydl:  # type: ignore[operator]
        info: Dict[str, Any] = ydl.extract_info(url, download=False)
    if not info:
        return {}
    return info


class YouTubeParser(BaseParser):
    """YouTube: метаданные через yt-dlp, субтитры — `youtube_transcript_api` (очистка как раньше).

    Ключ YouTube Data API не требуется. Параметр `youtube_api_key` в конструкторе
    оставлен для обратной совместимости с `create_parser_registry` и игнорируется.

    **Cookies (``YOUTUBE_COOKIE_FILE``):** тот же Netscape cookies.txt передаётся в **yt-dlp**
    и в **youtube_transcript_api** (список дорожек и ``get_transcript``), чтобы снизить отказы
    по капче/боту на стороне YouTube.

    Cookies для yt-dlp (обход проверки «Sign in to confirm you're not a bot»):

    1. Установите расширение браузера «Get cookies.txt LOCALLY».
    2. В отдельной вкладке инкогнито войдите в аккаунт YouTube.
    3. Экспортируйте cookies в файл ``youtube_cookies.txt`` (формат Netscape).
    4. Укажите путь к файлу в переменной окружения ``YOUTUBE_COOKIE_FILE``.

    После первой ошибки yt-dlp при использовании файла cookies путь сбрасывается в памяти
    парсера до перезапуска процесса — чтобы не дергать заведомо проблемный файл на каждом URL.

    Порядок запросов: субтитры (transcript-api, с retry при 429) → при успешном тексте субтитров
    yt-dlp не вызывается; иначе метаданные через yt-dlp → при полном провале — HTML ``/watch``.
    """

    source_type: str = "youtube"
    MAX_TEXT_LENGTH: int = 50_000

    _ERR_NO_TEXT = "Не удалось извлечь текст из видео"
    _YOUTUBE_RATE_LIMIT_USER_MESSAGE = "YouTube временно ограничил доступ. Попробуйте позже."
    _SUBTITLE_RETRY_DELAYS_SEC: Tuple[float, float] = (2.0, 4.0)

    def __init__(self, youtube_api_key: str = "", cookie_file: str | None = None) -> None:
        # API-ключ не используется (было только для YouTube Data API).
        _ = (youtube_api_key or "").strip()
        raw = (cookie_file or "").strip()
        self.cookie_file: str | None = raw if raw else None

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

    def _parse_sync(self, url: str) -> str:
        video_id = _extract_youtube_video_id(url)
        if not video_id or len(video_id) != 11:
            raise ValueError("Некорректный YouTube URL")

        logger.info("🎬 Обнаружено YouTube-видео: %s", video_id)

        cookie_path = self._resolved_cookie_file()

        # Субтитры первыми (retry при 429): меньше шансов «прожечь» лимит yt-dlp сразу.
        sub_text, _sub_lang, sub_reason = self._load_subtitles_with_retry(video_id, cookie_path)

        info: Optional[Dict[str, Any]] = None
        ytdlp_ok = False
        title_raw = ""
        desc_raw = ""
        tags_list: List[Any] = []

        if sub_text.strip():
            logger.info(
                "📼 Субтитры есть — yt-dlp для этого URL не вызываем; заголовок/описание с HTML watch",
            )
            raw_title, raw_desc = self._youtube_watch_page_description(
                url.strip(),
                video_id,
                strict_rate_limit=False,
            )
            title_raw = raw_title or ""
            desc_raw = raw_desc or ""
            tags_list = []
        else:
            try:
                info = _yt_dlp_extract_metadata(url.strip(), cookie_path)
                ytdlp_ok = True
            except Exception as exc:  # noqa: BLE001
                if cookie_path:
                    logger.warning(
                        "⚠️ yt-dlp с cookies не удалось (%s), повторяем без cookies",
                        exc,
                    )
                    self._disable_ytdlp_cookies_for_session()
                    try:
                        info = _yt_dlp_extract_metadata(url.strip(), None)
                        ytdlp_ok = True
                    except Exception as exc2:  # noqa: BLE001
                        logger.error("❌ Не удалось получить данные через yt-dlp: %s", exc2)
                        info = None
                        ytdlp_ok = False
                else:
                    logger.error("❌ Не удалось получить данные через yt-dlp: %s", exc)
                    info = None
                    ytdlp_ok = False

            if info:
                title_raw = (info.get("title") or info.get("fulltitle") or "")
                if not isinstance(title_raw, str):
                    title_raw = str(title_raw)
                desc_raw = info.get("description") or ""
                if not isinstance(desc_raw, str):
                    desc_raw = str(desc_raw) if desc_raw is not None else ""
                raw_tags = info.get("tags")
                if isinstance(raw_tags, list):
                    tags_list = raw_tags
                elif raw_tags is not None:
                    tags_list = [raw_tags]

            if ytdlp_ok and info:
                dlen = len(desc_raw) if desc_raw else 0
                t_short = (title_raw[:200] + "…") if len(title_raw) > 200 else title_raw
                logger.info(
                    "📦 Данные получены через yt-dlp: title=%r, description=%s символов",
                    t_short,
                    dlen,
                )

            if not ytdlp_ok:
                logger.warning(
                    "⚠️ yt-dlp не дал метаданные (субтитры: %s)",
                    (sub_reason[:120] + "…") if len(sub_reason) > 120 else sub_reason or "пусто",
                )

        clean_title = _strip_youtube_title_text(title_raw)
        clean_desc = _strip_youtube_description_text(desc_raw) if desc_raw else ""
        tag_line = _format_tags_line(tags_list)

        parts: List[str] = []
        if clean_title:
            parts.append(clean_title)
        if clean_desc:
            parts.append(clean_desc)
        if sub_text:
            parts.append(sub_text)
        if tag_line:
            parts.append("Теги: " + tag_line)

        if not any(p.strip() for p in parts):
            logger.warning(
                "⚠️ Контента всё ещё нет — последний фолбэк: HTML страницы /watch",
            )
            raw_title, raw_desc = self._youtube_watch_page_description(
                url.strip(),
                video_id,
                strict_rate_limit=True,
            )
            fb_title = _strip_youtube_title_text(raw_title) if raw_title else ""
            fb_desc = _strip_youtube_description_text(raw_desc) if raw_desc else ""
            if fb_title:
                parts.append(fb_title)
            if fb_desc:
                parts.append(fb_desc)
            if fb_title or fb_desc:
                logger.info(
                    "📄 Фолбэк HTML watch: заголовок %s симв., описание %s симв.",
                    len(fb_title),
                    len(fb_desc),
                )

        if not any(p.strip() for p in parts):
            logger.error("❌ %s", self._ERR_NO_TEXT)
            raise RuntimeError(self._ERR_NO_TEXT)

        text = "\n\n".join(p for p in parts if p and p.strip())
        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]
        return text

    def _load_subtitles_with_retry(
        self, video_id: str, transcript_cookies: Optional[str]
    ) -> Tuple[str, str, str]:
        """Субтитры с повторами при лимите YouTube (429). После исчерпания — сообщение пользователю."""
        delays = self._SUBTITLE_RETRY_DELAYS_SEC
        for attempt in range(3):
            try:
                return self._load_subtitles(video_id, transcript_cookies)
            except Exception as exc:
                if not _exc_is_transcript_rate_limit(exc):
                    raise
                if attempt >= 2:
                    logger.error(
                        "❌ transcript-api: лимит запросов после 3 попыток: %s",
                        exc,
                    )
                    raise RuntimeError(self._YOUTUBE_RATE_LIMIT_USER_MESSAGE) from exc
                wait = delays[attempt]
                logger.warning(
                    "⚠️ transcript-api: лимит (429), пауза %.0f с перед повтором (%s/3)",
                    wait,
                    attempt + 2,
                )
                time.sleep(wait)

    def _disable_ytdlp_cookies_for_session(self) -> None:
        """Не использовать файл cookies для yt-dlp до перезапуска процесса бота."""
        if not self.cookie_file:
            return
        logger.info(
            "ℹ️ Путь cookies для yt-dlp сброшен до перезапуска "
            "(ошибка запроса с cookies; следующие видео — без этого файла)",
        )
        self.cookie_file = None

    def _resolved_cookie_file(self) -> Optional[str]:
        """Путь к cookies.txt для yt-dlp или ``None`` (файл необязателен).

        Без пути или при отсутствии файла парсер ведёт себя как раньше.
        """
        if not self.cookie_file:
            return None
        expanded = os.path.abspath(os.path.expanduser(self.cookie_file.strip()))
        if os.path.isfile(expanded):
            logger.info("🍪 Используются cookies из %s", expanded)
            return expanded
        logger.warning(
            "⚠️ Файл cookies не найден: %s, работаем без cookies",
            self.cookie_file,
        )
        return None

    def _youtube_watch_page_description(
        self,
        url: str,
        video_id: str,
        *,
        strict_rate_limit: bool = True,
    ) -> Tuple[str, str]:
        """Публичная страница ``/watch``: заголовок и описание из HTML (без yt-dlp / transcript-api).

        Признаки страницы-заглушки (429 / лимит IP) обрабатываются до парсинга meta.
        Если ``strict_rate_limit=True`` и обнаружена блокировка — ``RuntimeError`` с текстом для пользователя.

        Args:
            strict_rate_limit: Если False и страница похожа на блокировку — вернуть ``("", "")`` без исключения
                (например, когда уже есть текст субтитров).
        """
        _ = url
        msg = self._YOUTUBE_RATE_LIMIT_USER_MESSAGE
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        req = urllib.request.Request(watch_url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw_bytes = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                logger.warning("⚠️ Фолбэк HTML watch: HTTP 429 для %s", watch_url)
                if strict_rate_limit:
                    raise RuntimeError(msg) from exc
                return "", ""
            logger.warning("⚠️ Фолбэк HTML watch: HTTP %s для %s", exc.code, watch_url)
            return "", ""
        except urllib.error.URLError as exc:
            logger.warning("⚠️ Фолбэк HTML watch: ошибка URL %s (%s)", watch_url, exc)
            return "", ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Фолбэк HTML watch: не удалось загрузить страницу: %s", exc)
            return "", ""

        html_text = raw_bytes.decode("utf-8", errors="replace")

        if _youtube_watch_html_looks_rate_limited(html_text):
            logger.warning(
                "⚠️ Фолбэк HTML watch: похоже на блокировку/заглушку YouTube (429/sorry), не парсим og:-мета (%s)",
                watch_url,
            )
            if strict_rate_limit:
                raise RuntimeError(msg)
            return "", ""

        soup = BeautifulSoup(html_text, "lxml")

        title_s = ""
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            title_s = html.unescape(str(og_title["content"]).strip())
        if not title_s:
            meta_title = soup.find("meta", attrs={"name": "title"})
            if meta_title and meta_title.get("content"):
                title_s = html.unescape(str(meta_title["content"]).strip())

        desc_s = ""
        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            desc_s = html.unescape(str(og_desc["content"]).strip())
        if not desc_s:
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                desc_s = html.unescape(str(meta_desc["content"]).strip())

        if not title_s and not desc_s:
            logger.warning(
                "⚠️ Фолбэк HTML watch: в разметке не найдены og:title / og:description (%s)",
                watch_url,
            )
        return title_s, desc_s

    def _load_subtitles(self, video_id: str, transcript_cookies: Optional[str] = None) -> Tuple[str, str, str]:
        """Субтитры: (текст, язык, причина; пустая строка = OK).

        При лимите YouTube (429 / :class:`~youtube_transcript_api._errors.TooManyRequests`)
        исключение пробрасывается наверх для :meth:`_load_subtitles_with_retry`.

        ``transcript_cookies`` — тот же Netscape cookies.txt, что и для yt-dlp (если задан).
        """
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (  # type: ignore[import-not-found]
            NoTranscriptFound,
            TooManyRequests,
            TranscriptsDisabled,
            VideoUnavailable,
            YouTubeRequestFailed,
        )

        try:
            tlist = _youtube_list_transcripts(video_id, transcript_cookies)
        except TooManyRequests:
            raise
        except VideoUnavailable as exc:
            msg = f"ролик недоступен (VideoUnavailable: {exc})"
            logger.warning("⚠️ %s", msg)
            return self._fallback_get_transcript_only(
                video_id,
                YouTubeTranscriptApi,
                reason_prefix=msg,
                transcript_cookies=transcript_cookies,
            )
        except TranscriptsDisabled as exc:
            reason = f"субтитры отключены автором: {exc}"
            logger.warning("⚠️ %s", reason)
            return "", "", reason
        except YouTubeRequestFailed as exc:
            if _exc_is_transcript_rate_limit(exc):
                raise
            err_t = f"{type(exc).__name__}: {exc}"
            logger.error("❌ list_transcripts: %s", err_t)
            return self._fallback_get_transcript_only(
                video_id,
                YouTubeTranscriptApi,
                reason_prefix=err_t,
                transcript_cookies=transcript_cookies,
            )
        except Exception as exc:  # noqa: BLE001
            if _exc_is_transcript_rate_limit(exc):
                raise
            err_t = f"{type(exc).__name__}: {exc}"
            logger.error("❌ list_transcripts: %s", err_t)
            return self._fallback_get_transcript_only(
                video_id,
                YouTubeTranscriptApi,
                reason_prefix=err_t,
                transcript_cookies=transcript_cookies,
            )

        all_tr: List[Any] = []
        try:
            for t in tlist:  # type: ignore[operator]
                all_tr.append(t)
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ обход списка дорожек: %s", exc)
            all_tr = []

        if not all_tr:
            reason = "YouTube не вернул ни одной дорожки субтитров"
            logger.warning("⚠️ %s", reason)
            return "", "", reason

        try:
            tr_obj, sub_kind, pick_reason = _select_transcript_track(
                tlist, all_tr, NoTranscriptFound
            )
        except RuntimeError as exc:
            logger.error("❌ выбор дорожки субтитров: %s", exc)
            return "", "", str(exc)
        lang = _transcript_lang_code(tr_obj)
        try:
            raw = tr_obj.fetch()
        except TooManyRequests:
            raise
        except YouTubeRequestFailed as exc:
            if _exc_is_transcript_rate_limit(exc):
                raise
            logger.error("❌ fetch() субтитров: %s: %s", type(exc).__name__, exc)
            return "", "", f"сбой fetch: {type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001
            if _exc_is_transcript_rate_limit(exc):
                raise
            logger.error("❌ fetch() субтитров: %s: %s", type(exc).__name__, exc)
            return "", "", f"сбой fetch: {type(exc).__name__}"

        sub_text = _clean_subtitle_fetch(raw)
        if not sub_text:
            r = f"текст дорожки пуст ({pick_reason})"
            logger.warning("⚠️ %s", r)
            return "", "", r

        nchars = self._format_number(len(sub_text))
        if sub_kind == "auto":
            logger.info("✅ Автосубтитры (%s), %s символов", lang, nchars)
        else:
            logger.info("⚠️ Ручные субтитры (%s), %s символов", lang, nchars)
        logger.debug("Метка дорожки: %s", pick_reason)
        return sub_text, lang, ""

    def _fallback_get_transcript_only(
        self,
        video_id: str,
        ytt: Any,
        reason_prefix: str,
        transcript_cookies: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        try:
            try:
                raw = ytt.get_transcript(
                    video_id,
                    languages=["ru", "en"],
                    proxies=None,
                    cookies=transcript_cookies,
                )  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                raw = ytt.get_transcript(
                    video_id,
                    proxies=None,
                    cookies=transcript_cookies,
                )  # type: ignore[attr-defined]
        except Exception as exc2:  # noqa: BLE001
            if _exc_is_transcript_rate_limit(exc2):
                raise
            r = f"{reason_prefix}; get_transcript: {type(exc2).__name__}: {exc2}"
            logger.error("❌ %s", r)
            return "", "", r
        sub = _clean_subtitle_fetch(raw)
        if sub:
            logger.info(
                "ℹ️ Субтитры (get_transcript, без деталей ASR/ручн.), %s символов",
                self._format_number(len(sub)),
            )
            return sub, "mixed", ""
        return "", "", f"{reason_prefix}; get_transcript вернул пустой текст"

    @staticmethod
    def _format_number(value: int) -> str:
        return f"{value:,}".replace(",", " ")


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
    """Реестр с YouTube (до Web). Поля ``youtube_api_key`` и связанные — для
    обратной совместимости; ``YouTubeParser`` ключ API не использует.
    Путь к cookies: ``cfg.youtube_cookie_file`` → ``YouTubeParser``.
    """
    if cfg is None:
        from config import config as _cfg
        cfg = _cfg
    api_key = str(getattr(cfg, "youtube_api_key", "") or "")
    yt_cookie_raw = str(getattr(cfg, "youtube_cookie_file", "") or "").strip()
    yt_cookie: str | None = yt_cookie_raw if yt_cookie_raw else None
    registry = ParserRegistry()
    registry.register(YouTubeParser(youtube_api_key=api_key, cookie_file=yt_cookie))
    registry.register(WebParser())
    return registry


# --------------------------------------------------------------------------- #
# __main__
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    async def _test() -> None:
        assert YouTubeParser.can_parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert YouTubeParser.can_parse("https://youtu.be/dQw4w9WgXcQ")
        assert YouTubeParser.can_parse("https://m.youtube.com/shorts/abcdefghijk1")
        assert not YouTubeParser.can_parse("https://eda.ru/recept/123")
        assert not YouTubeParser.can_parse("")
        assert _extract_youtube_video_id("https://www.youtube.com/watch?v=short") == ""
        print("✅ YouTube can_parse / extract ok")

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

        yurl = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        class _Cfg:
            youtube_api_key = ""
        reg = create_parser_registry(_Cfg())
        try:
            tyt = await reg.parse(yurl)
            print(f"✅ YouTube: {len(tyt)} символов")
            if tyt:
                print(f"📝 Начало: {tyt[:200]!r}…")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  YouTube тест: {exc}")

        try:
            text = await registry.parse("https://eda.ru/recepty/supy/klassicheskij-borshh-34567")
            print(f"✅ Eda: {len(text)} символов")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Eda: {exc}")

    asyncio.run(_test())
