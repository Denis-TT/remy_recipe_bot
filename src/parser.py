"""
Модуль парсеров рецептов Remy Bot.

* `BaseParser` — абстрактный контракт (`can_parse`, `parse`, `source_type`).
* `WebParser` — обычные HTTP(S)-страницы с рецептами.
* `YouTubeParser` — заголовок и описание через YouTube Data API v3; субтитры — Apify Actor
  ``pintostudio~youtube-transcript-scraper`` (при наличии ``APIFY_API_TOKEN``).
* `InstagramParser` — Reels и посты через Apify Actor ``crawlerbros~instagram-transcript-scraper`` (ввод ``videoUrls``).
* `ParserRegistry` / `create_parser_registry` — маршрутизация и фабрика.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Optional
from urllib.parse import urljoin

if TYPE_CHECKING:
    from .storage.supabase_storage import SupabaseStorage

import aiohttp
from bs4 import BeautifulSoup
from config import config as _remy_config
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from readability import Document as _ReadabilityDocument
except Exception:  # noqa: BLE001
    _ReadabilityDocument = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Изображения рецептов (том /images на Railway)
# --------------------------------------------------------------------------- #

IMAGES_DIR: str = str(_remy_config.images_dir or "/images").rstrip("/") or "/images"

HF_IMAGE_MODEL_ID = "prompthero/openjourney-v4"
HF_INFERENCE_URL = f"https://api-inference.huggingface.co/models/{HF_IMAGE_MODEL_ID}"


def ensure_images_dir() -> None:
    """Гарантировать существование каталога для сохранённых изображений."""
    global IMAGES_DIR
    try:
        os.makedirs(IMAGES_DIR, mode=0o755, exist_ok=True)
    except OSError as exc:
        env_explicit = bool(
            (os.environ.get("IMAGES_DIR") or os.environ.get("REMY_IMAGES_DIR") or "").strip()
        )
        if not env_explicit and IMAGES_DIR == "/images":
            fb = os.path.join(tempfile.gettempdir(), "remy_recipe_bot", "images")
            try:
                os.makedirs(fb, mode=0o755, exist_ok=True)
                IMAGES_DIR = fb
                logger.info(
                    "Каталог /images недоступен, изображения сохраняются в %s",
                    IMAGES_DIR,
                )
            except OSError as exc2:
                logger.warning(
                    "Не удалось создать каталог изображений %s: %s; запасной %s: %s",
                    "/images",
                    exc,
                    fb,
                    exc2,
                )
        else:
            logger.warning("Не удалось создать каталог изображений %s: %s", IMAGES_DIR, exc)


@dataclass
class ParseResult:
    """Результат парсинга: сырой текст и опциональный путь к локальному изображению."""

    text: str
    image_url: Optional[str] = None


async def _generate_and_save_image(title: str, *, hf_api_key: str) -> Optional[str]:
    """Сгенерировать изображение через Hugging Face Inference API и сохранить под ``IMAGES_DIR``."""
    key = (hf_api_key or "").strip()
    if not key:
        logger.info("Генерация изображений отключена (нет HF_API_KEY)")
        return None

    ensure_images_dir()
    base = (title or "").strip() or "delicious meal"
    prompt = (
        f"Professional food photography of {base}, restaurant plating, "
        "natural lighting, high detail, appetizing"
    )
    headers = {"Authorization": f"Bearer {key}"}
    payload = {"inputs": prompt}
    timeout = aiohttp.ClientTimeout(total=30)

    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.post(HF_INFERENCE_URL, json=payload) as resp:
                    body = await resp.read()
                    if resp.status == 200 and body:
                        ct = (resp.headers.get("Content-Type") or "").lower()
                        if "application/json" in ct:
                            logger.warning(
                                "Попытка %d: HF вернул JSON вместо изображения: %s",
                                attempt + 1,
                                body[:300].decode("utf-8", errors="replace"),
                            )
                        else:
                            uid = uuid.uuid4().hex
                            path = os.path.join(IMAGES_DIR, f"{uid}.jpg")
                            with open(path, "wb") as f:
                                f.write(body)
                            logger.info("Изображение сгенерировано через Hugging Face: %s", path)
                            return path
                    elif resp.status >= 400:
                        logger.warning(
                            "Попытка %d: Hugging Face HTTP %s: %s",
                            attempt + 1,
                            resp.status,
                            body[:300].decode("utf-8", errors="replace"),
                        )
                    else:
                        logger.warning("Попытка %d: пустой ответ Hugging Face", attempt + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("Попытка %d генерации изображения не удалась: %s", attempt + 1, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Попытка %d генерации изображения не удалась: %s", attempt + 1, exc)
        await asyncio.sleep(2)

    logger.error("Не удалось сгенерировать изображение для %s после 3 попыток", base[:200])
    return None


async def attach_recipe_image(
    recipe_data: dict[str, Any],
    local_path: str,
    storage: Optional["SupabaseStorage"] = None,
) -> None:
    """Привязать локальный файл к рецепту; при возможности загрузить в Supabase Storage."""
    path = (local_path or "").strip()
    if not path or not os.path.isfile(path):
        return

    recipe_data["image_path"] = path

    if storage is None:
        recipe_data["image_url"] = path
        return

    public_url = await storage.upload_image(path)
    if public_url:
        recipe_data["image_url"] = public_url
    else:
        recipe_data["image_url"] = path


# --------------------------------------------------------------------------- #
# Абстрактный базовый класс
# --------------------------------------------------------------------------- #


class BaseParser(ABC):
    """Абстрактный базовый класс для всех парсеров рецептов."""

    generate_image_default: bool = False

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Тип источника (``website``, ``youtube`` и т. д.)."""

    @staticmethod
    @abstractmethod
    def can_parse(url: str) -> bool:
        """Быстрая проверка URL (без сети)."""

    @abstractmethod
    async def parse(self, url: str) -> ParseResult:
        """Извлечь сырой текст рецепта (и при наличии — путь или URL изображения)."""

    async def generate_image_if_needed(
        self,
        recipe_data: dict[str, Any],
        *,
        hf_api_key: str = "",
        storage: Optional["SupabaseStorage"] = None,
    ) -> dict[str, Any]:
        """Если источнику нужна AI-картинка — сгенерировать, загрузить в Storage, записать ``image_url``."""
        if not self.generate_image_default:
            return recipe_data
        if recipe_data.get("image_url") or recipe_data.get("image_path"):
            return recipe_data
        raw = (recipe_data.get("raw_text") or "").strip()
        prompt = raw.split("\n", 1)[0][:400] if raw else "Вкусное домашнее блюдо, еда на тарелке, фуд-фото"
        path = await _generate_and_save_image(prompt, hf_api_key=hf_api_key)
        if path:
            await attach_recipe_image(recipe_data, path, storage)
        return recipe_data


# --------------------------------------------------------------------------- #
# Реализация: обычные веб-страницы
# --------------------------------------------------------------------------- #


class WebParser(BaseParser):
    """Парсер веб-страниц: aiohttp + readability + BeautifulSoup."""

    source_type: str = "website"
    generate_image_default: bool = False
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

    def __init__(self) -> None:
        self.last_image_url: Optional[str] = None

    @staticmethod
    def can_parse(url: str) -> bool:
        if not isinstance(url, str):
            return False
        return url.startswith(("http://", "https://"))

    async def parse(self, url: str) -> ParseResult:
        if not self.can_parse(url):
            raise ValueError(f"WebParser не поддерживает URL: {url!r}")

        logger.info("🔍 Начинаю парсинг: %s", url)

        self.last_image_url = None
        html = await self._fetch(url)
        image_url = await self._extract_og_image(html, url)
        text = self._extract_text(html)

        if not text:
            logger.error("❌ Ошибка парсинга %s: пустой контент", url)
            raise RuntimeError("Страница не содержит текста")

        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]

        logger.info("📄 Извлечено %s символов текста", self._format_number(len(text)))
        return ParseResult(text=text, image_url=image_url)

    def _extract_og_image_url(self, html: str, page_url: str) -> Optional[str]:
        """URL титульного изображения из ``og:image`` (абсолютный)."""
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("meta", property="og:image")
        if tag is None or not tag.get("content"):
            tag = soup.find("meta", attrs={"name": "og:image"})
        if tag is None or not tag.get("content"):
            return None
        raw = str(tag.get("content") or "").strip()
        if not raw:
            return None
        return urljoin(page_url, raw)

    async def _extract_og_image(self, html: str, page_url: str) -> Optional[str]:
        """Найти og:image, скачать в ``/images/<uuid>.jpg``, вернуть путь или ``None``."""
        img_url = self._extract_og_image_url(html, page_url)
        self.last_image_url = img_url
        if not img_url:
            logger.warning("og:image не найден для %s", page_url)
            return None
        ensure_images_dir()
        dest = os.path.join(IMAGES_DIR, f"{uuid.uuid4().hex}.jpg")
        try:
            ok = await self._download_binary_to_path(img_url, dest)
            if ok:
                logger.info("Изображение сохранено: %s", dest)
                return dest
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("Не удалось загрузить og:image %s: %s", img_url, exc)
        return None

    async def _download_binary_to_path(self, file_url: str, dest_path: str) -> bool:
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)
        max_bytes = 8 * 1024 * 1024
        async with aiohttp.ClientSession(timeout=timeout, headers=self.HEADERS) as session:
            async with session.get(file_url, allow_redirects=True) as response:
                if response.status >= 400:
                    logger.warning("og:image HTTP %s для %s", response.status, file_url)
                    return False
                data = await response.read()
        if len(data) > max_bytes:
            logger.warning("og:image слишком большой (%s байт)", len(data))
            return False
        with open(dest_path, "wb") as f:
            f.write(data)
        return True

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
# Instagram: краулер `crawlerbros~instagram-transcript-scraper` (ввод: ``videoUrls``: [url]).
APIFY_INSTAGRAM_TRANSCRIPT_ACTOR = "crawlerbros~instagram-transcript-scraper"
# Дольше, чем YouTube — транскрипт Instagram может обрабатываться минутами.
APIFY_INSTAGRAM_WAIT_FINISH_SEC = 300
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


def _apify_raw_segment_list(payload: Any, _depth: int = 0) -> List[str]:
    """Плоский список текстов сегментов (без склейки), в т.ч. для ``item["data"]`` в списках."""
    max_depth = 6
    if _depth > max_depth or payload is None:
        return []
    try:
        if isinstance(payload, str):
            s = payload.strip()
            return [s] if s else []
        if isinstance(payload, dict):
            if "data" in payload and payload.get("data") is not None:
                return _apify_raw_segment_list(payload["data"], _depth + 1)
            tr = payload.get("transcript")
            if isinstance(tr, list) and tr:
                return _apify_segments_from_transcript_list(tr)
            for key in ("text", "subtitleText", "chunk"):
                v = payload.get(key)
                try:
                    if isinstance(v, str) and v.strip():
                        return [v.strip()]
                    if v is not None and not isinstance(v, (dict, list)):
                        s = str(v).strip()
                        if s:
                            return [s]
                except (TypeError, ValueError, AttributeError):
                    continue
            return []
        if isinstance(payload, list):
            acc: List[str] = []
            for it in payload:
                if isinstance(it, str):
                    if it.strip():
                        acc.append(it.strip())
                elif isinstance(it, dict):
                    acc.extend(_apify_raw_segment_list(it, _depth + 1))
            return acc
        return []
    except (TypeError, ValueError, AttributeError):
        return []


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
            if inner is None:
                logger.info("Apify: субтитры не найдены (пустой data)")
                return "", 0, "empty_data"
            if isinstance(inner, list) and len(inner) == 0:
                logger.info("Apify: субтитры не найдены (пустой data)")
                return "", 0, "empty_data"
            if isinstance(inner, dict) and len(inner) == 0:
                logger.info("Apify: субтитры не найдены (пустой data)")
                return "", 0, "empty_data"
            text, n, inner_tag = _apify_extract_subtitles_payload(inner, _depth + 1)
            if n > 0 and text:
                return text, n, f"data→{inner_tag}"
            # Пустые субтитры внутри data (например [{ "data": [] }]) — не «формат не распознан»
            if n == 0 and inner_tag in ("empty_data", "list[empty_data]", "empty_list"):
                return "", 0, f"data→{inner_tag}"

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

        # Список (иногда Apify отдаёт [[{...}]] — сначала снимаем лишнюю обёртку)
        if isinstance(payload, list):
            pl: Any = payload
            while len(pl) == 1 and isinstance(pl[0], list):
                pl = pl[0]
            payload = pl
            if not payload:
                return "", 0, "empty_list"

            parts: List[str] = []
            list_only_strings = True
            used_list_item_data = False
            saw_empty_data = False
            for item in payload:
                try:
                    if isinstance(item, str):
                        if item.strip():
                            parts.append(item.strip())
                        continue
                    if isinstance(item, list):
                        sub_t, sub_n, sub_tag = _apify_extract_subtitles_payload(item, _depth + 1)
                        list_only_strings = False
                        if sub_n > 0 and sub_t:
                            parts.append(sub_t)
                        elif sub_tag in ("empty_data", "list[empty_data]", "empty_list"):
                            saw_empty_data = True
                        continue
                    list_only_strings = False
                    if not isinstance(item, dict):
                        continue
                    if "data" in item:
                        data_val = item.get("data")
                        if data_val is None:
                            saw_empty_data = True
                            continue
                        if isinstance(data_val, list) and len(data_val) == 0:
                            saw_empty_data = True
                            continue
                        if isinstance(data_val, dict) and len(data_val) == 0:
                            saw_empty_data = True
                            continue
                        if data_val is not None:
                            nested = _apify_raw_segment_list(data_val, _depth + 1)
                            if nested:
                                used_list_item_data = True
                                parts.extend(nested)
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

            if not parts and saw_empty_data:
                logger.info("Apify: субтитры не найдены (пустой data)")
                return "", 0, "list[empty_data]"

            if parts:
                if list_only_strings:
                    fmt = "list[str]"
                elif used_list_item_data:
                    fmt = "list[object.data]"
                else:
                    fmt = "list[object]"
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
    generate_image_default: bool = True
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

    async def parse(self, url: str) -> ParseResult:
        if not self.can_parse(url):
            raise ValueError(f"YouTubeParser не поддерживает URL: {url!r}")
        text = await asyncio.to_thread(self._parse_sync, url)
        return ParseResult(text=text)

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
        title_for_text = clean_title or (raw_title or "").strip()
        desc_for_text = clean_desc or (
            (raw_desc or "").replace("\r\n", "\n").strip() if raw_desc else ""
        )

        title_preview = raw_title[:50] + ("..." if len(raw_title) > 50 else "")
        logger.info(
            'Получены данные через YouTube Data API: "%s"',
            title_preview,
        )

        sub_text = self._fetch_subtitles_apify(video_id)

        parts_out: List[str] = []
        if title_for_text:
            parts_out.append(title_for_text)
        if desc_for_text:
            parts_out.append(desc_for_text)
        core = "\n\n".join(parts_out)
        if sub_text:
            text = f"{core}\n\n{sub_text}" if core else sub_text
        else:
            text = core
            if core.strip():
                logger.info("Данные YouTube Data API переданы без субтитров")

        if not text.strip():
            logger.error("❌ Не удалось извлечь текст из видео (пустой заголовок и описание)")
            raise RuntimeError("Не удалось извлечь текст из видео")

        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]
        return text


def _instagram_safe_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _instagram_transcript_to_text(transcript: Any) -> str:
    if transcript is None:
        return ""
    if isinstance(transcript, str):
        return transcript.strip()
    if isinstance(transcript, dict):
        t = transcript.get("text")
        return _instagram_safe_str(t)
    if isinstance(transcript, list):
        parts: List[str] = []
        for seg in transcript:
            if isinstance(seg, dict):
                parts.append(_instagram_safe_str(seg.get("text")))
            elif isinstance(seg, str) and seg.strip():
                parts.append(seg.strip())
        return " ".join(p for p in parts if p)
    return str(transcript).strip()


def _instagram_unwrap_record(obj: dict) -> dict:
    inner = obj.get("data")
    if isinstance(inner, dict):
        return inner
    return obj


def _instagram_fields_from_record(rec: dict) -> tuple[str, str, str]:
    title = _instagram_safe_str(
        rec.get("title")
        or rec.get("captionTitle")
        or rec.get("videoTitle")
        or rec.get("postTitle")
        or rec.get("headline")
    )
    description = _instagram_safe_str(
        rec.get("description")
        or rec.get("caption")
        or rec.get("postDescription")
        or rec.get("post_text")
        or rec.get("text")
    )
    transcript_text = _instagram_transcript_to_text(rec.get("transcript"))
    if not transcript_text:
        transcript_text = _instagram_safe_str(
            rec.get("fullText")
            or rec.get("transcriptText")
            or rec.get("subtitleText")
        )
    return title, description, transcript_text


def _instagram_items_look_like_segment_rows(rows: List[dict]) -> bool:
    """Строки датасета `crawlerbros~instagram-transcript-scraper` (сегменты)."""
    if not rows:
        return False
    d0 = rows[0]
    return any(
        k in d0
        for k in ("fullText", "segmentText", "segmentIndex", "totalSegments", "transcriptionMethod")
    )


def _instagram_fields_from_segment_rows(rows: List[dict]) -> tuple[str, str, str]:
    """Собрать title / description / транскрипт из нескольких строк сегментов."""
    ok = [r for r in rows if isinstance(r, dict) and not (str(r.get("errMsg") or "").strip())]
    if not ok:
        ok = [r for r in rows if isinstance(r, dict)]
    if not ok:
        return "", "", ""

    first = ok[0]
    caption = _instagram_safe_str(first.get("title"))
    user = _instagram_safe_str(first.get("userName"))
    full_name = _instagram_safe_str(first.get("userFullName"))
    desc_parts = [p for p in (f"@{user}" if user else "", full_name) if p]
    description = " · ".join(desc_parts)
    title = caption if caption else (f"@{user}" if user else "")

    full_text = _instagram_safe_str(first.get("fullText"))
    if not full_text:
        indexed: List[tuple[tuple[int, int], str]] = []
        for ord_i, r in enumerate(ok):
            st = _instagram_safe_str(r.get("segmentText"))
            if not st:
                continue
            idx_raw = r.get("segmentIndex")
            try:
                idx_int = int(idx_raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                idx_int = ord_i
            indexed.append(((idx_int, ord_i), st))
        indexed.sort(key=lambda x: x[0])
        full_text = " ".join(s for _, s in indexed)

    return title, description, full_text


def _instagram_extract_from_items(items_payload: Any) -> Optional[tuple[str, str, str]]:
    """Разобрать тело GET dataset items: один объект, legacy или список сегментов."""
    if items_payload is None:
        return None
    if isinstance(items_payload, dict):
        rec = _instagram_unwrap_record(items_payload)
        return _instagram_fields_from_record(rec) if rec else None

    if isinstance(items_payload, list):
        dicts: List[dict] = []
        for x in items_payload:
            if isinstance(x, dict):
                u = _instagram_unwrap_record(x)
                if u:
                    dicts.append(u)
        if not dicts:
            return None
        if _instagram_items_look_like_segment_rows(dicts):
            return _instagram_fields_from_segment_rows(dicts)
        for d in dicts:
            tup = _instagram_fields_from_record(d)
            if tup[0] or tup[1] or tup[2]:
                return tup
        return _instagram_fields_from_record(dicts[0])

    return None


class InstagramParser(BaseParser):
    """Instagram Reels и посты через Apify ``crawlerbros~instagram-transcript-scraper``."""

    source_type: str = "instagram"
    generate_image_default: bool = True
    MAX_TEXT_LENGTH: int = 50_000

    def __init__(self, apify_api_token: str = "") -> None:
        self._apify_api_token = (apify_api_token or "").strip()

    @staticmethod
    def _extract_shortcode(url: str) -> str:
        m = re.search(
            r"instagram\.com/(?:reel|p)/([A-Za-z0-9_-]+)",
            (url or "").strip(),
            re.IGNORECASE,
        )
        return m.group(1) if m else ""

    @staticmethod
    def can_parse(url: str) -> bool:
        if not isinstance(url, str) or not url.strip():
            return False
        u = url.lower()
        return "instagram.com/reel/" in u or "instagram.com/p/" in u

    async def parse(self, url: str) -> ParseResult:
        if not self.can_parse(url):
            raise ValueError(f"InstagramParser не поддерживает URL: {url!r}")
        text = await asyncio.to_thread(self._parse_sync, url)
        return ParseResult(text=text)

    def _fetch_via_apify(self, url: str) -> tuple[str, str, str]:
        """Вернуть (title, description, transcript_text) или три пустые строки при сбое."""
        token = (self._apify_api_token or "").strip()
        if not token:
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", ""

        run_url = (
            f"https://api.apify.com/v2/acts/{APIFY_INSTAGRAM_TRANSCRIPT_ACTOR}/runs"
            f"?waitForFinish={APIFY_INSTAGRAM_WAIT_FINISH_SEC}"
        )
        envelope = _apify_http_json(
            "POST",
            run_url,
            token,
            {"videoUrls": [url]},
        )
        if not isinstance(envelope, dict):
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", ""

        run = envelope.get("data")
        if not isinstance(run, dict):
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", ""

        run_id = run.get("id")
        if not run_id:
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", ""

        items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?format=json"
        items_payload = _apify_http_json("GET", items_url, token, None)
        triple = _instagram_extract_from_items(items_payload)
        if not triple:
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", ""

        title, description, transcript_text = triple
        if not (title or description or transcript_text):
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", ""

        logger.info("Получены данные через Apify Instagram Transcript Scraper")
        return title, description, transcript_text

    def _parse_sync(self, url: str) -> str:
        shortcode = self._extract_shortcode(url)
        if not shortcode:
            raise ValueError("Некорректный Instagram URL")

        logger.info("📸 Обнаружено Instagram видео: %s", shortcode)

        title, description, transcript_text = self._fetch_via_apify(url)

        chunks = [title, description, transcript_text]
        text = "\n\n".join(c for c in chunks if c and str(c).strip())

        if not text.strip():
            logger.error("Ошибка получения Instagram субтитров")
            raise RuntimeError("Не удалось извлечь текст из Instagram (пустой ответ Apify)")

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

    async def parse(self, url: str) -> ParseResult:
        parser = self.get_parser(url)
        if parser is None:
            raise ValueError(f"Не удалось найти парсер для URL: {url}")
        logger.info("🔍 Парсинг через %s: %s", parser.source_type, url[:80])
        return await parser.parse(url)

    @property
    def parsers(self) -> List[BaseParser]:
        return list(self._parsers)


def create_parser_registry(cfg: Optional[object] = None) -> ParserRegistry:
    """Реестр: YouTube, Instagram, Web. Передаются ``youtube_api_key`` и ``apify_api_token``."""
    if cfg is None:
        from config import config as _cfg
        cfg = _cfg
    api_key = str(getattr(cfg, "youtube_api_key", "") or "")
    apify_tok = str(getattr(cfg, "apify_api_token", "") or "")
    registry = ParserRegistry()
    registry.register(YouTubeParser(youtube_api_key=api_key, apify_api_token=apify_tok))
    registry.register(InstagramParser(apify_api_token=apify_tok))
    registry.register(WebParser())
    return registry


# --------------------------------------------------------------------------- #
# __main__
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import os
    import sys
    import tempfile
    from unittest.mock import AsyncMock, MagicMock, patch

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
        t_ld, n_ld = _apify_dataset_payload_to_subtitle_text(
            [
                {
                    "data": [
                        {"start": "0", "dur": "1", "text": "первый"},
                        {"start": "1", "dur": "1", "text": "второй"},
                    ]
                }
            ]
        )
        assert n_ld == 2 and "первый" in t_ld and "второй" in t_ld
        t_ed, n_ed = _apify_dataset_payload_to_subtitle_text([{"data": []}])
        assert n_ed == 0 and t_ed == ""
        t_ed2, n_ed2 = _apify_dataset_payload_to_subtitle_text({"data": []})
        assert n_ed2 == 0 and t_ed2 == ""
        t_ed3, n_ed3 = _apify_dataset_payload_to_subtitle_text([[{"data": []}]])
        assert n_ed3 == 0 and t_ed3 == ""
        t_ed4, n_ed4 = _apify_dataset_payload_to_subtitle_text({"data": {}})
        assert n_ed4 == 0 and t_ed4 == ""
        t_junk, n_junk = _apify_dataset_payload_to_subtitle_text({"foo": 1})
        assert n_junk == 0 and t_junk == ""
        print("✅ YouTube can_parse / extract + Apify форматы субтитров")

        assert WebParser.generate_image_default is False
        assert YouTubeParser.generate_image_default is True
        assert InstagramParser.generate_image_default is True
        mod = sys.modules[__name__]
        wp = WebParser()
        og_u = wp._extract_og_image_url(
            '<html><meta property="og:image" content="https://img.test/hero.jpg"/></html>',
            "https://site.ru/rec/1",
        )
        assert og_u == "https://img.test/hero.jpg"
        og_rel = wp._extract_og_image_url(
            '<meta property="og:image" content="/pics/x.png"/>',
            "https://eda.ru/base/article",
        )
        assert og_rel == "https://eda.ru/pics/x.png"
        print("✅ WebParser og:image URL")

        wp_miss = WebParser()
        assert await wp_miss._extract_og_image("<html></html>", "https://nope.test/x") is None
        assert wp_miss.last_image_url is None
        print("✅ WebParser last_image_url при отсутствии og:image")

        td = tempfile.mkdtemp()
        with patch.object(mod, "IMAGES_DIR", td):

            async def _fake_dl(_u: str, dest: str) -> bool:
                with open(dest, "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xd9")
                return True

            html_og = '<head><meta property="og:image" content="https://static/x.jpg"/></head>'
            wpo = WebParser()
            with patch.object(WebParser, "_download_binary_to_path", side_effect=_fake_dl):
                saved = await wpo._extract_og_image(html_og, "https://www.example.com/r")
            assert saved is not None and saved.startswith(td) and saved.endswith(".jpg")
            assert os.path.isfile(saved)
            assert wpo.last_image_url == "https://static/x.jpg"
        print("✅ WebParser og:image скачивание (мок)")

        assert await mod._generate_and_save_image("тест", hf_api_key="") is None

        td_pol = tempfile.mkdtemp()
        with patch.object(mod, "IMAGES_DIR", td_pol):
            mock_resp = AsyncMock()
            mock_resp.status = 200
            mock_resp.read = AsyncMock(return_value=b"\xff\xd8\xff\xd9")
            mock_resp.headers = {"Content-Type": "image/jpeg"}
            post_ctx = MagicMock()
            post_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
            post_ctx.__aexit__ = AsyncMock(return_value=None)
            mock_sess = MagicMock()
            mock_sess.post = MagicMock(return_value=post_ctx)
            sess_ctx = MagicMock()
            sess_ctx.__aenter__ = AsyncMock(return_value=mock_sess)
            sess_ctx.__aexit__ = AsyncMock(return_value=None)
            with patch.object(aiohttp, "ClientSession", return_value=sess_ctx):
                hf_path = await mod._generate_and_save_image("Борщ тест", hf_api_key="hf_test")
            assert hf_path is not None and hf_path.startswith(td_pol) and hf_path.endswith(".jpg")
            assert os.path.isfile(hf_path)
        print("✅ Hugging Face _generate_and_save_image (мок aiohttp)")

        rd: dict = {"raw_text": "Суп дня", "image_url": None}
        await WebParser().generate_image_if_needed(rd, hf_api_key="hf")
        assert rd.get("image_url") is None
        with patch.object(mod, "_generate_and_save_image", new_callable=AsyncMock) as gm:
            mock_path = os.path.join(td_pol, "mock.jpg")
            gm.return_value = mock_path
            yt_dummy = YouTubeParser(youtube_api_key="x", apify_api_token="")
            rd2 = {"raw_text": "Плов узбекский", "image_url": None}
            await yt_dummy.generate_image_if_needed(rd2, hf_api_key="hf")
            assert rd2.get("image_path") == mock_path
            assert rd2.get("image_url") == mock_path
        print("✅ generate_image_if_needed / флаги парсеров")

        assert InstagramParser.can_parse("https://www.instagram.com/reel/ABCxyz12/")
        assert InstagramParser.can_parse("https://instagram.com/p/XYZ_ab-1/")
        assert InstagramParser._extract_shortcode(
            "https://www.instagram.com/reel/AbCd123/?utm=x",
        ) == "AbCd123"
        assert InstagramParser._extract_shortcode("https://www.instagram.com/p/xY9_/") == "xY9_"
        assert not InstagramParser.can_parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert not InstagramParser.can_parse("")
        ig_actor_ds = [
            {
                "url": "https://www.instagram.com/reel/TEST123/",
                "code": "TEST123",
                "title": "Шоколадный торт #рецепт",
                "userName": "baker_maria",
                "userFullName": "Мария П.",
                "fullText": "Смешать яйца и сахар. Добавить муку.",
                "segmentIndex": 0,
                "segmentText": "Смешать яйца и сахар.",
                "errMsg": "",
            },
            {
                "title": "Шоколадный торт #рецепт",
                "userName": "baker_maria",
                "userFullName": "Мария П.",
                "fullText": "Смешать яйца и сахар. Добавить муку.",
                "segmentIndex": 1,
                "segmentText": "Добавить муку.",
                "errMsg": "",
            },
        ]
        ig_t, ig_d, ig_tr = _instagram_extract_from_items(ig_actor_ds)
        assert "торт" in ig_t
        assert "baker_maria" in ig_d
        assert "муку" in ig_tr
        print("✅ Instagram can_parse / shortcode + мок crawlerbros dataset")

        with patch.object(
            InstagramParser,
            "_fetch_via_apify",
            return_value=("IG Title", "Описание и ингредиенты", "текст субтитров"),
        ):
            ig_out = await InstagramParser(apify_api_token="dummy").parse(
                "https://www.instagram.com/reel/xyz123xxxxx/",
            )
        assert "IG Title" in ig_out.text and "Описание" in ig_out.text and "субтитров" in ig_out.text
        print("✅ Instagram parse() с моком Apify")

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
            assert "Mock Recipe" in parsed.text
            assert "Ingredients" in parsed.text
            assert "Apify subtitle" in parsed.text
            print("✅ parse() с моком Data API и Apify")

        with patch.object(this_mod, "build") as mock_build_ns, patch.object(
            YouTubeParser,
            "_fetch_subtitles_apify",
            return_value="",
        ):
            mock_yt_ns = MagicMock()
            mock_build_ns.return_value = mock_yt_ns
            mock_req_ns = MagicMock()
            mock_yt_ns.videos.return_value.list.return_value = mock_req_ns
            mock_req_ns.execute.return_value = {
                "items": [
                    {
                        "snippet": {
                            "title": "Video Sans Subs",
                            "description": "Ингредиенты: мука 200 г, вода",
                        },
                        "contentDetails": {"duration": "PT1M"},
                    }
                ]
            }
            parsed_ns = await YouTubeParser(
                youtube_api_key="test-key",
                apify_api_token="dummy-apify",
            ).parse("https://www.youtube.com/watch?v=abcdefghijk")
            assert "Video Sans Subs" in parsed_ns.text
            assert "Ингредиенты" in parsed_ns.text
        print("✅ YouTube: данные Data API без субтитров Apify")

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
            assert "T2" in out_nop.text
            print("✅ без APIFY_API_TOKEN нет HTTP-запросов к Apify")

        _live_key = os.getenv("YOUTUBE_API_KEY", "").strip()
        _live_apify = os.getenv("APIFY_API_TOKEN", "").strip()
        if _live_key and _live_apify:
            try:
                live_text = await YouTubeParser(
                    youtube_api_key=_live_key,
                    apify_api_token=_live_apify,
                ).parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
                print(f"✅ YouTube live (Data API + Apify): {len(live_text.text)} символов")
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  YouTube live: {exc}")
        elif _live_key:
            print("⚠️  Только YOUTUBE_API_KEY — пропуск live Apify (нужен APIFY_API_TOKEN)")

        registry = create_parser_registry()
        yt_p = registry.get_parser("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert yt_p is not None and getattr(yt_p, "source_type", None) == "youtube"
        ig_p = registry.get_parser("https://www.instagram.com/reel/abcd1234567/")
        assert ig_p is not None and getattr(ig_p, "source_type", None) == "instagram"
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
            print(f"✅ Eda: {len(text.text)} символов")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Eda: {exc}")

    import asyncio as _asyncio

    _asyncio.run(_test())
