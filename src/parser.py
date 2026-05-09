"""
Модуль парсеров рецептов Remy Bot.

* `BaseParser` — абстрактный контракт (`can_parse`, `parse`, `source_type`).
* `WebParser` — обычные HTTP(S)-страницы с рецептами.
* `YouTubeParser` — заголовок и описание через YouTube Data API v3; субтитры — Apify Actor
  ``pintostudio~youtube-transcript-scraper`` (при наличии ``APIFY_API_TOKEN``).
* `ParserRegistry` / `create_parser_registry` — маршрутизация и фабрика.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, List, Optional

import aiohttp
from bs4 import BeautifulSoup
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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


# Apify Actor (субтитры YouTube). Официальный список items: GET /v2/actor-runs/{runId}/dataset/items
APIFY_TRANSCRIPT_ACTOR = "pintostudio~youtube-transcript-scraper"
APIFY_WAIT_FINISH_SEC = 120


def _apify_http_json(
    method: str,
    url: str,
    token: str,
    body: Optional[dict[str, Any]] = None,
    timeout_sec: float = 180.0,
) -> Any:
    """Синхронный JSON-запрос к Apify API. При ошибке возвращает None (ошибка залогирована)."""
    token = (token or "").strip()
    if not token:
        logger.error("Apify: токен пустой — заголовок Authorization не сформирован")
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data: Optional[bytes] = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    raw = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if not raw.strip():
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.error(
                    "Apify: ответ не является JSON (%s). Первые 500 символов: %r",
                    exc,
                    raw[:500],
                )
                return None
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        if exc.code == 401:
            logger.error(
                "Apify HTTP 401 Unauthorized: неверный или пустой API-токен. "
                "Проверьте APIFY_API_TOKEN в .env (токен из Apify Console → "
                "Settings → API & Integrations, без лишних пробелов и префикса Bearer). "
                "Ответ сервера: %s",
                err[:600],
            )
        else:
            logger.error("Apify HTTP %s для %s: %s", exc.code, url, err[:800])
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("Apify запрос не удался (%s): %s", url, exc)
        return None


def _apify_collapse_subtitle_text(parts: List[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _apify_segments_from_transcript_list(tr_list: list) -> List[str]:
    """Извлечь строки из списка сегментов ``[{text: ...}, ...]``."""
    out: List[str] = []
    for seg in tr_list:
        try:
            if isinstance(seg, dict):
                t = seg.get("text")
                if t is not None and str(t).strip():
                    out.append(str(t).strip())
            elif isinstance(seg, str) and seg.strip():
                out.append(seg.strip())
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _apify_extract_subtitles_payload(
    payload: Any,
    _depth: int = 0,
) -> tuple[str, int, str]:
    """Разобрать тело ответа dataset Apify в субтитры.

    Поддерживаются вложенность ``data``, ``transcript``, списки объектов/строк,
    одиночный объект с ``text``. При успехе: (текст, число сегментов, метка формата).

    Не бросает исключений при мусорном входе — возвращает (\"\", 0, \"unrecognized\").
    """
    tag_unrecognized = "unrecognized"
    max_depth = 6
    if _depth > max_depth:
        logger.warning("Apify субтитры: слишком глубокая вложенность (%s), разбор остановлен", _depth)
        return "", 0, tag_unrecognized
    try:
        if payload is None:
            return "", 0, tag_unrecognized

        # Обёртка { "data": ... }
        if isinstance(payload, dict) and "data" in payload:
            inner = payload.get("data")
            text, n, inner_tag = _apify_extract_subtitles_payload(inner, _depth + 1)
            if n > 0 and text:
                return text, n, f"data→{inner_tag}"

        # Объект с transcript
        if isinstance(payload, dict):
            tr = payload.get("transcript")
            if isinstance(tr, list) and tr:
                parts = _apify_segments_from_transcript_list(tr)
                if parts:
                    return _apify_collapse_subtitle_text(parts), len(parts), "object.transcript"

            for key in ("text", "subtitleText", "chunk"):
                v = payload.get(key)
                try:
                    if isinstance(v, str) and v.strip():
                        return _apify_collapse_subtitle_text([v.strip()]), 1, f"object.{key}"
                    if v is not None and not isinstance(v, (dict, list)):
                        s = str(v).strip()
                        if s:
                            return _apify_collapse_subtitle_text([s]), 1, f"object.{key}"
                except (TypeError, ValueError, AttributeError):
                    continue

        # Список
        if isinstance(payload, list):
            if not payload:
                return "", 0, "empty_list"

            parts: List[str] = []
            list_only_strings = True
            for item in payload:
                try:
                    if isinstance(item, str):
                        if item.strip():
                            parts.append(item.strip())
                        continue
                    list_only_strings = False
                    if not isinstance(item, dict):
                        continue
                    tr = item.get("transcript")
                    if isinstance(tr, list) and tr:
                        parts.extend(_apify_segments_from_transcript_list(tr))
                        continue
                    for key in ("text", "subtitleText", "chunk"):
                        v = item.get(key)
                        if isinstance(v, str) and v.strip():
                            parts.append(v.strip())
                            break
                        if v is not None and not isinstance(v, (dict, list)):
                            s = str(v).strip()
                            if s:
                                parts.append(s)
                                break
                except (TypeError, ValueError, AttributeError):
                    continue

            if parts:
                fmt = "list[str]" if list_only_strings else "list[object]"
                return _apify_collapse_subtitle_text(parts), len(parts), fmt

        logger.warning(
            "Apify субтитры: формат не распознан (тип %s, превью: %r)",
            type(payload).__name__,
            repr(payload)[:220],
        )
        return "", 0, tag_unrecognized
    except Exception as exc:  # noqa: BLE001
        logger.warning("Apify субтитры: неожиданная ошибка разбора: %s", exc)
        return "", 0, tag_unrecognized


def _apify_dataset_payload_to_subtitle_text(payload: Any) -> tuple[str, int]:
    """Внешняя обёртка: (полный текст, число сегментов) и лог успешного формата."""
    text, n, fmt = _apify_extract_subtitles_payload(payload, 0)
    if n > 0 and text:
        logger.info(
            "Apify: распознан формат «%s», получено %s сегментов субтитров",
            fmt,
            n,
        )
    return text, n


class YouTubeParser(BaseParser):
    """YouTube: Data API v3 — заголовок и описание; субтитры — Apify (опционально).

    Биллинг Apify (ориентиры для планирования; актуальные цены — на apify.com/pricing и у Actor):
    бесплатный tier даёт около $5 кредита в месяц; один запуск YouTube Transcript Scraper
    обычно порядка $0.02 за видео; на месячный кредит ориентировочно выходит 250+ видео.
    """

    source_type: str = "youtube"
    MAX_TEXT_LENGTH: int = 50_000

    def __init__(
        self,
        youtube_api_key: str = "",
        apify_api_token: str = "",
    ) -> None:
        self.youtube_api_key = (youtube_api_key or "").strip()
        self._apify_api_token = (apify_api_token or "").strip()

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

    def _fetch_subtitles_apify(self, video_id: str) -> str:
        """Субтитры через Apify POST run + GET dataset items (без HTTP, если токен пуст)."""
        token = (self._apify_api_token or "").strip()
        if not token:
            logger.info("APIFY_API_TOKEN не задан — запрос субтитров в Apify не выполняется")
            return ""
        if len(token) < 8:
            logger.warning(
                "APIFY_API_TOKEN слишком короткий (%s симв.) — ожидается полный Apify API token",
                len(token),
            )

        run_url = (
            f"https://api.apify.com/v2/acts/{APIFY_TRANSCRIPT_ACTOR}/runs"
            f"?waitForFinish={APIFY_WAIT_FINISH_SEC}"
        )
        envelope = _apify_http_json(
            "POST",
            run_url,
            token,
            {"videoUrl": f"https://www.youtube.com/watch?v={video_id}"},
        )
        if not isinstance(envelope, dict):
            return ""

        run = envelope.get("data")
        if not isinstance(run, dict):
            return ""
        run_id = run.get("id")
        if not run_id:
            logger.warning("Apify: в ответе run нет id")
            return ""

        items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?format=json"
        items_payload = _apify_http_json("GET", items_url, token, None)
        texts, n_segments = _apify_dataset_payload_to_subtitle_text(items_payload)

        if texts:
            logger.info("Получены субтитры через Apify (%s символов)", len(texts))
        else:
            logger.info("Apify не вернул текста субтитров для видео %s", video_id)
        return texts

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

        sub_text = self._fetch_subtitles_apify(video_id)

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
    """Реестр с YouTube (до Web). Передаются ``youtube_api_key`` и ``apify_api_token``."""
    if cfg is None:
        from config import config as _cfg
        cfg = _cfg
    api_key = str(getattr(cfg, "youtube_api_key", "") or "")
    apify_tok = str(getattr(cfg, "apify_api_token", "") or "")
    registry = ParserRegistry()
    registry.register(YouTubeParser(youtube_api_key=api_key, apify_api_token=apify_tok))
    registry.register(WebParser())
    return registry


# --------------------------------------------------------------------------- #
# __main__
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import os
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
        tx, n = _apify_dataset_payload_to_subtitle_text(
            [
                {
                    "transcript": [
                        {"start": 0, "dur": 1, "text": " Hello "},
                        {"start": 1, "dur": 1, "text": "world"},
                    ]
                }
            ]
        )
        assert n == 2 and "Hello" in tx and "world" in tx
        tx2, n2 = _apify_dataset_payload_to_subtitle_text([{"text": "flat caption"}])
        assert n2 == 1 and "flat" in tx2
        tx3, n3 = _apify_dataset_payload_to_subtitle_text([])
        assert n3 == 0 and tx3 == ""
        t_tr, n_tr = _apify_dataset_payload_to_subtitle_text(
            {"transcript": [{"text": "новый"}, {"text": "формат"}]}
        )
        assert n_tr == 2 and "новый" in t_tr
        t_arr, n_arr = _apify_dataset_payload_to_subtitle_text(["один", "два"])
        assert n_arr == 2 and "один" in t_arr and "два" in t_arr
        t_one, n_one = _apify_dataset_payload_to_subtitle_text({"text": "одиночка"})
        assert n_one == 1 and t_one == "одиночка"
        t_wr, n_wr = _apify_dataset_payload_to_subtitle_text(
            {"data": [{"text": "в"}, {"text": "data"}]}
        )
        assert n_wr == 2
        t_wr2, n_wr2 = _apify_dataset_payload_to_subtitle_text({"data": ["x", "y"]})
        assert n_wr2 == 2
        t_junk, n_junk = _apify_dataset_payload_to_subtitle_text({"foo": 1})
        assert n_junk == 0 and t_junk == ""
        print("✅ YouTube can_parse / extract + Apify форматы субтитров")

        no_key = YouTubeParser(youtube_api_key="")
        try:
            await no_key.parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        except RuntimeError as exc:
            if "YOUTUBE_API_KEY" not in str(exc):
                raise AssertionError(f"Ожидалось сообщение про ключ: {exc}") from exc
            print("✅ Без YouTube API-ключа: понятный RuntimeError")
        else:
            raise AssertionError("Ожидался RuntimeError без YOUTUBE_API_KEY")

        this_mod = sys.modules[__name__]
        with patch.object(this_mod, "build") as mock_build, patch.object(
            YouTubeParser,
            "_fetch_subtitles_apify",
            return_value="Mock Apify subtitle text",
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
            parsed = await YouTubeParser(
                youtube_api_key="test-key",
                apify_api_token="dummy-apify",
            ).parse("https://www.youtube.com/watch?v=abcdefghijk")
            assert "Mock Recipe" in parsed
            assert "Ingredients" in parsed
            assert "Apify subtitle" in parsed
            print("✅ parse() с моком Data API и Apify")

        with patch("urllib.request.urlopen") as urlopen_mock:
            mock_yt = MagicMock()
            mock_build = MagicMock(return_value=mock_yt)
            mock_req = MagicMock()
            mock_yt.videos.return_value.list.return_value = mock_req
            mock_req.execute.return_value = {
                "items": [
                    {
                        "snippet": {"title": "T2", "description": "D2"},
                        "contentDetails": {},
                    }
                ]
            }
            with patch.object(this_mod, "build", mock_build):
                out_nop = await YouTubeParser(youtube_api_key="k", apify_api_token="").parse(
                    "https://www.youtube.com/watch?v=abcdefghijk"
                )
            urlopen_mock.assert_not_called()
            assert "T2" in out_nop
            print("✅ без APIFY_API_TOKEN нет HTTP-запросов к Apify")

        _live_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        _live_apify = os.getenv("APIFY_API_TOKEN", "").strip()
        if _live_key and _live_apify:
            try:
                live_text = await YouTubeParser(
                    youtube_api_key=_live_key,
                    apify_api_token=_live_apify,
                ).parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
                print(f"✅ YouTube live (Data API + Apify): {len(live_text)} символов")
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  YouTube live: {exc}")
        elif _live_key:
            print("⚠️  Только YOUTUBE_API_KEY — пропуск live Apify (нужен APIFY_API_TOKEN)")

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
