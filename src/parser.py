"""
Модуль парсеров рецептов Remy Bot.

* `BaseParser` — абстрактный контракт (`can_parse`, `parse`, `source_type`).
* `WebParser` — обычные HTTP(S)-страницы с рецептами.
* `YouTubeParser` — заголовок и описание через YouTube Data API v3; субтитры — Apify Actor
  ``pintostudio~youtube-transcript-scraper`` (при наличии ``APIFY_API_TOKEN``).
* `InstagramParser` — Reels и посты через Apify Actor
  ``apple_yang~instagram-transcripts-scraper`` (ввод ``videoUrl``).
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

# Модель FLUX.1-dev — через ``InferenceClient(provider="fal-ai")``, не serverless Inference API.
HF_FLUX_MODEL_ID = "black-forest-labs/FLUX.1-dev"
HF_FLUX_PROVIDER = "fal-ai"


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


def _flux_text_to_image_and_save_sync(prompt: str, hf_api_key: str, dest_path: str) -> None:
    """Синхронный вызов FLUX.1-dev через ``InferenceClient`` (провайдер fal-ai)."""
    from huggingface_hub import InferenceClient

    client = InferenceClient(provider=HF_FLUX_PROVIDER, api_key=hf_api_key)
    image = client.text_to_image(prompt=prompt, model=HF_FLUX_MODEL_ID)
    if image is None:
        raise RuntimeError("FLUX.1-dev вернул пустое изображение")
    image.save(dest_path, "JPEG", quality=90)


async def _generate_and_save_image(title: str, *, hf_api_key: str) -> Optional[str]:
    """Сгенерировать изображение через FLUX.1-dev (fal-ai) и сохранить под ``IMAGES_DIR``."""
    key = (hf_api_key or "").strip()
    if not key:
        logger.info("Генерация изображений отключена (нет HF_API_KEY)")
        return None

    ensure_images_dir()
    dish_title = (title or "").strip() or "delicious meal"
    prompt = (
        f"Professional food photography of {dish_title}, soft natural lighting, "
        "shallow depth of field, restaurant quality presentation, high detail, "
        "delicious and appetizing, shot from above on a beautiful plate"
    )

    for attempt in range(3):
        path = os.path.join(IMAGES_DIR, f"{uuid.uuid4().hex}.jpg")
        try:
            await asyncio.to_thread(_flux_text_to_image_and_save_sync, prompt, key, path)
            logger.info("Изображение сгенерировано через FLUX.1-dev (fal-ai): %s", path)
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("Попытка %d генерации FLUX.1-dev не удалась: %s", attempt + 1, exc)
            if os.path.isfile(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
        await asyncio.sleep(2)

    logger.error("Не удалось сгенерировать изображение для %s после 3 попыток", dish_title[:200])
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
# Instagram: https://apify.com/apple_yang/instagram-transcripts-scraper
APIFY_INSTAGRAM_TRANSCRIPT_ACTOR = "apple_yang~instagram-transcripts-scraper"
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
                            nested_parts = (
                                _apify_segments_from_transcript_list(data_val)
                                if isinstance(data_val, list)
                                else []
                            )
                            if nested_parts:
                                used_list_item_data = True
                                parts.extend(nested_parts)
                                continue
                            nested_text, nested_n, _nested_tag = _apify_extract_subtitles_payload(
                                data_val,
                                _depth + 1,
                            )
                            if nested_n > 0 and nested_text:
                                used_list_item_data = True
                                parts.append(nested_text)
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


def _instagram_segments_to_text(segments: Any) -> str:
    """Склеить ``segments`` нового Instagram Actor в один текст."""
    if not isinstance(segments, list):
        return ""
    parts: List[str] = []
    for seg in segments:
        if isinstance(seg, dict):
            text = _instagram_safe_str(seg.get("text"))
            if text:
                parts.append(text)
        elif isinstance(seg, str) and seg.strip():
            parts.append(seg.strip())
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _instagram_author_description(user_name: str, full_name: str) -> str:
    user = user_name.strip()
    full = full_name.strip()
    if user and not user.startswith("@"):
        user = f"@{user}"
    if user and full:
        return f"{user} ({full})"
    return user or full


def _instagram_extract_from_item(item: Any) -> tuple[str, str, str, str]:
    """Разобрать один объект Actor ``apple_yang~instagram-transcripts-scraper``."""
    if not isinstance(item, dict):
        return "", "", "", ""

    err_msg = _instagram_safe_str(item.get("errMsg"))
    title = _instagram_safe_str(item.get("title"))
    description = _instagram_author_description(
        _instagram_safe_str(item.get("userName")),
        _instagram_safe_str(item.get("userFullName")),
    )
    img_url = _instagram_safe_str(item.get("img"))

    if err_msg:
        logger.error("Ошибка получения Instagram субтитров: %s", err_msg)
        return title, description, "", img_url

    transcript_text = _instagram_safe_str(item.get("text"))
    if not transcript_text:
        transcript_text = _instagram_segments_to_text(item.get("segments"))

    if not transcript_text:
        logger.warning("Apify не вернул текста субтитров для Instagram")

    return title, description, transcript_text, img_url


class InstagramParser(BaseParser):
    """Instagram Reels и посты через Apify ``apple_yang~instagram-transcripts-scraper``.

    Actor: https://apify.com/apple_yang/instagram-transcripts-scraper
    """

    source_type: str = "instagram"
    generate_image_default: bool = False
    MAX_TEXT_LENGTH: int = 50_000
    TIMEOUT_SECONDS: float = 30.0
    HEADERS: dict = WebParser.HEADERS

    def __init__(self, apify_api_token: str = "", instagram_session_id: str = "") -> None:
        self._apify_api_token = (apify_api_token or "").strip()
        self.sessionid = (instagram_session_id or "").strip()

    @staticmethod
    def _extract_shortcode(url: str) -> str:
        m = re.search(
            r"(?:www\.|m\.)?instagram\.com/(?:reel|reels|p)/([A-Za-z0-9_-]+)",
            (url or "").strip(),
            re.IGNORECASE,
        )
        return m.group(1) if m else ""

    @staticmethod
    def can_parse(url: str) -> bool:
        if not isinstance(url, str) or not url.strip():
            return False
        u = url.lower().strip()
        return bool(
            re.search(
                r"(?:^|://)(?:www\.|m\.)?instagram\.com/(?:reel|reels|p|share/(?:reel|p))/",
                u,
                re.IGNORECASE,
            )
        )

    async def parse(self, url: str) -> ParseResult:
        if not self.can_parse(url):
            raise ValueError(f"InstagramParser не поддерживает URL: {url!r}")
        text, img_url = await asyncio.to_thread(self._parse_sync, url)
        image_path = await self._download_instagram_image(img_url)
        return ParseResult(text=text, image_url=image_path)

    async def _download_instagram_image(self, img_url: str) -> Optional[str]:
        """Скачать ``img`` из Instagram Actor в ``IMAGES_DIR`` и вернуть локальный путь."""
        url = (img_url or "").strip()
        if not url:
            logger.info("Instagram не предоставил изображение (поле img пусто)")
            return None
        if not url.startswith(("http://", "https://")):
            logger.warning("Не удалось скачать изображение Instagram: %s", url)
            return None

        ensure_images_dir()
        dest = os.path.join(IMAGES_DIR, f"{uuid.uuid4().hex}.jpg")
        try:
            ok = await self._download_binary_to_path(url, dest)
            if ok:
                logger.info("Изображение Instagram сохранено: %s", dest)
                return dest
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("Не удалось скачать изображение Instagram: %s", url)
            logger.debug("Ошибка скачивания Instagram img: %s", exc)
        if os.path.isfile(dest):
            try:
                os.unlink(dest)
            except OSError:
                pass
        return None

    async def _download_binary_to_path(self, file_url: str, dest_path: str) -> bool:
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)
        max_bytes = 8 * 1024 * 1024
        async with aiohttp.ClientSession(timeout=timeout, headers=self.HEADERS) as session:
            async with session.get(file_url, allow_redirects=True) as response:
                if response.status >= 400:
                    logger.warning("Не удалось скачать изображение Instagram: %s", file_url)
                    return False
                data = await response.read()
        if len(data) > max_bytes:
            logger.warning("Не удалось скачать изображение Instagram: %s", file_url)
            return False
        with open(dest_path, "wb") as f:
            f.write(data)
        return True

    def _fetch_via_apify(self, url: str) -> tuple[str, str, str, str]:
        """Вернуть title / description / transcript / img или пустые строки при сбое."""
        token = (self._apify_api_token or "").strip()
        if not token:
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", "", ""

        run_url = (
            f"https://api.apify.com/v2/acts/{APIFY_INSTAGRAM_TRANSCRIPT_ACTOR}/runs"
            f"?waitForFinish={APIFY_INSTAGRAM_WAIT_FINISH_SEC}"
        )
        body = {"videoUrl": url}
        if self.sessionid:
            logger.info("Используется Instagram sessionid")
            body["sessionid"] = self.sessionid
        envelope = _apify_http_json(
            "POST",
            run_url,
            token,
            body,
        )
        if not isinstance(envelope, dict):
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", "", ""

        run = envelope.get("data")
        if not isinstance(run, dict):
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", "", ""

        run_id = run.get("id")
        if not run_id:
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", "", ""

        items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?format=json"
        items_payload = _apify_http_json("GET", items_url, token, None)
        item: Any = items_payload
        if isinstance(items_payload, list):
            item = items_payload[0] if items_payload else None
        title, description, transcript_text, img_url = _instagram_extract_from_item(item)
        if not (title or description or transcript_text):
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", "", ""

        logger.info("Получены данные через Apify Instagram Transcripts Scraper")
        return title, description, transcript_text, img_url

    def _parse_sync(self, url: str) -> tuple[str, str]:
        shortcode = self._extract_shortcode(url)
        is_share_url = bool(
            re.search(
                r"(?:^|://)(?:www\.|m\.)?instagram\.com/share/(?:reel|p)/",
                (url or "").strip(),
                re.IGNORECASE,
            )
        )
        if not shortcode and not is_share_url:
            raise ValueError("Некорректный Instagram URL")

        logger.info("📸 Обнаружено Instagram видео: %s", shortcode or "share-url")

        title, description, transcript_text, img_url = self._fetch_via_apify(url)
        if not (title or description or transcript_text):
            logger.error("Ошибка получения Instagram субтитров")
            raise RuntimeError("Не удалось извлечь текст из Instagram (пустой ответ Apify)")

        chunks = [title, description, transcript_text]
        text = "\n\n".join(c for c in chunks if c and str(c).strip())

        if not text.strip():
            logger.error("Ошибка получения Instagram субтитров")
            raise RuntimeError("Не удалось извлечь текст из Instagram (пустой ответ Apify)")

        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]
        return text, img_url


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
    """Реестр: YouTube, Instagram, Web. Передаются ключи API и Instagram sessionid."""
    if cfg is None:
        from config import config as _cfg
        cfg = _cfg
    api_key = str(getattr(cfg, "youtube_api_key", "") or "")
    apify_tok = str(getattr(cfg, "apify_api_token", "") or "")
    instagram_session_id = str(getattr(cfg, "instagram_session_id", "") or "")
    registry = ParserRegistry()
    registry.register(YouTubeParser(youtube_api_key=api_key, apify_api_token=apify_tok))
    registry.register(
        InstagramParser(
            apify_api_token=apify_tok,
            instagram_session_id=instagram_session_id,
        )
    )
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
        assert InstagramParser.generate_image_default is False
        mod = sys.modules[__name__]
        this_mod = mod
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
        assert mod.HF_FLUX_MODEL_ID == "black-forest-labs/FLUX.1-dev"
        assert mod.HF_FLUX_PROVIDER == "fal-ai"

        td_pol = tempfile.mkdtemp()
        with patch.object(mod, "IMAGES_DIR", td_pol):
            from PIL import Image as PILImage

            mock_pil = PILImage.new("RGB", (64, 64), color=(200, 100, 50))
            mock_client = MagicMock()
            mock_client.text_to_image.return_value = mock_pil

            with patch("huggingface_hub.InferenceClient", return_value=mock_client) as mock_cls:
                hf_path = await mod._generate_and_save_image("Борщ тест", hf_api_key="hf_test")
            assert hf_path is not None and hf_path.startswith(td_pol) and hf_path.endswith(".jpg")
            assert os.path.isfile(hf_path)
            mock_cls.assert_called_with(provider="fal-ai", api_key="hf_test")
            mock_client.text_to_image.assert_called_once()
            call_kw = mock_client.text_to_image.call_args.kwargs
            assert call_kw.get("model") == "black-forest-labs/FLUX.1-dev"
            assert "Борщ" in call_kw.get("prompt", "")

            mock_client.text_to_image.side_effect = RuntimeError("fal-ai недоступен")
            with patch("huggingface_hub.InferenceClient", return_value=mock_client):
                path_fail = await mod._generate_and_save_image("Суп", hf_api_key="hf_test")
            assert path_fail is None
        print("✅ FLUX.1-dev _generate_and_save_image (мок InferenceClient)")

        rd: dict = {"raw_text": "Суп дня", "image_url": None}
        await WebParser().generate_image_if_needed(rd, hf_api_key="hf")
        assert rd.get("image_url") is None
        with patch.object(mod, "_generate_and_save_image", new_callable=AsyncMock) as gm:
            mock_path = os.path.join(td_pol, "mock.jpg")
            with open(mock_path, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")
            gm.return_value = mock_path
            yt_dummy = YouTubeParser(youtube_api_key="x", apify_api_token="")
            rd2 = {"raw_text": "Плов узбекский", "image_url": None}
            await yt_dummy.generate_image_if_needed(rd2, hf_api_key="hf")
            assert rd2.get("image_path") == mock_path
            assert rd2.get("image_url") == mock_path
        print("✅ generate_image_if_needed / флаги парсеров")

        assert InstagramParser.can_parse("https://www.instagram.com/reel/ABCxyz12/")
        assert InstagramParser.can_parse("https://m.instagram.com/reels/ABCxyz12/?igsh=x")
        assert InstagramParser.can_parse("https://instagram.com/p/XYZ_ab-1/")
        assert InstagramParser.can_parse("https://www.instagram.com/share/reel/BAABCxyz12/")
        assert InstagramParser._extract_shortcode(
            "https://www.instagram.com/reel/AbCd123/?utm=x",
        ) == "AbCd123"
        assert InstagramParser._extract_shortcode("https://m.instagram.com/reels/ReEl_42/") == "ReEl_42"
        assert InstagramParser._extract_shortcode("https://www.instagram.com/p/xY9_/") == "xY9_"
        assert not InstagramParser.can_parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert not InstagramParser.can_parse("")
        ig_actor_item = {
            "title": "Шоколадный торт #рецепт",
            "userName": "baker_maria",
            "userFullName": "Мария П.",
            "img": "https://cdn.example.test/recipe.jpg",
            "text": "Смешать яйца и сахар. Добавить муку.",
            "segments": [
                {"start": 0.0, "end": 3.1, "text": "Смешать яйца и сахар."},
                {"start": 3.2, "end": 5.4, "text": "Добавить муку."},
            ],
            "errMsg": "",
        }
        ig_t, ig_d, ig_tr, ig_img = _instagram_extract_from_item(ig_actor_item)
        assert "торт" in ig_t
        assert "baker_maria" in ig_d
        assert "муку" in ig_tr
        assert ig_img == "https://cdn.example.test/recipe.jpg"
        ig_seg_t, ig_seg_d, ig_seg_tr, ig_seg_img = _instagram_extract_from_item({
            "title": "Сырники",
            "userName": "chef",
            "segments": [
                {"start": 0, "end": 1, "text": "Берем творог."},
                {"start": 1, "end": 2, "text": "Жарим на сковороде."},
            ],
        })
        assert ig_seg_t == "Сырники"
        assert "@chef" in ig_seg_d
        assert "творог" in ig_seg_tr and "Жарим" in ig_seg_tr
        assert ig_seg_img == ""
        print("✅ Instagram can_parse / shortcode + мок apple_yang dataset")

        apify_calls: List[tuple[str, str, dict[str, Any] | None]] = []

        def _mock_ig_apify(method: str, req_url: str, token: str, body: Optional[dict[str, Any]] = None):
            apify_calls.append((method, req_url, body))
            if method == "POST":
                return {"data": {"id": "run-ig"}}
            return [ig_actor_item]

        with patch.object(this_mod, "_apify_http_json", side_effect=_mock_ig_apify):
            ig_live_mock = InstagramParser(
                apify_api_token="dummy-apify",
                instagram_session_id="session-test",
            )._fetch_via_apify("https://www.instagram.com/reel/TEST123/")
        assert "торт" in ig_live_mock[0]
        assert apify_calls[0][2] == {
            "videoUrl": "https://www.instagram.com/reel/TEST123/",
            "sessionid": "session-test",
        }

        td_ig = tempfile.mkdtemp()

        async def _fake_ig_image_download(_u: str, dest: str) -> bool:
            with open(dest, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")
            return True

        with patch.object(
            InstagramParser,
            "_fetch_via_apify",
            return_value=(
                "IG Title",
                "Описание и ингредиенты",
                "текст субтитров",
                "https://cdn.example.test/recipe.jpg",
            ),
        ), patch.object(
            InstagramParser,
            "_download_binary_to_path",
            side_effect=_fake_ig_image_download,
        ), patch.object(mod, "IMAGES_DIR", td_ig):
            ig_out = await InstagramParser(apify_api_token="dummy").parse(
                "https://www.instagram.com/reel/xyz123xxxxx/",
            )
        assert "IG Title" in ig_out.text and "Описание" in ig_out.text and "субтитров" in ig_out.text
        assert ig_out.image_url is not None and ig_out.image_url.startswith(td_ig)
        assert os.path.isfile(ig_out.image_url)
        no_img = await InstagramParser(apify_api_token="dummy")._download_instagram_image("")
        assert no_img is None
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
