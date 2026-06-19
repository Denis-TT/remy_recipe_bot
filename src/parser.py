"""
Модуль парсеров рецептов Remy Bot.

* `BaseParser` — абстрактный контракт (`can_parse`, `parse`, `source_type`).
* `WebParser` — обычные HTTP(S)-страницы с рецептами.
* `YouTubeParser` — yt-dlp метаданные + Whisper + публичные субтитры / Apify-fallback.
* `TikTokParser` — тот же пайплайн для TikTok.
* `InstagramParser` — Reels: yt-dlp + Whisper + Apify ``apple_yang~instagram-transcripts-scraper``.
* `ParserRegistry` / `create_parser_registry` — маршрутизация и фабрика.
"""

from __future__ import annotations

import asyncio
import contextlib
import html as html_lib
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Optional
from urllib.parse import urljoin

if TYPE_CHECKING:
    from .storage.supabase_storage import SupabaseStorage

import aiohttp
from bs4 import BeautifulSoup
from config import config as _remy_config

from .ytdlp_mixin import (
    YtdlpWhisperMixin,
    metadata_from_ytdlp_info,
    pick_best_thumbnail,
    safe_video_str,
    video_compose_text,
)

try:
    from readability import Document as _ReadabilityDocument
except Exception:  # noqa: BLE001
    _ReadabilityDocument = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

# Колбэк прогресса парсинга: (stage_id, detail). Используется InstagramParser
# для обновления статусного сообщения в Telegram.
ProgressCallback = Callable[[str, str], Awaitable[None]]


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
    """Парсер веб-страниц: aiohttp + readability + requests-html fallback."""

    source_type: str = "website"
    generate_image_default: bool = False
    MAX_TEXT_LENGTH: int = 50_000
    TIMEOUT_SECONDS: float = 30.0
    RENDER_TIMEOUT_SECONDS: int = 60
    MIN_USEFUL_TEXT_LENGTH: int = 500

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
        "Accept-Language": "ru-RU, ru;q=0.9",
    }

    MOBILE_HEADERS: dict = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        ),
        "Accept-Language": "ru-RU, ru;q=0.9",
    }

    _RECIPE_KEYWORDS_RE = re.compile(
        r"(ингредиент|рецепт|приготовлен|приготовление|способ приготовления)",
        re.IGNORECASE,
    )
    _TEXT_STOP_WORDS_RE = re.compile(
        r"(?:^|\b)(войти|регистрация|зарегистрироваться|подписаться|комментарий|"
        r"комментарии|поделиться|читать далее|реклама|личный кабинет|"
        r"политика конфиденциальности|условия использования)(?:\b|$)",
        re.IGNORECASE,
    )

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
        static_text = self._extract_text(html, stage="static (aiohttp)")
        text = static_text
        rendered_text = ""

        if self._has_enough_text(static_text):
            logger.info("Статический парсинг успешен: %s символов", len(static_text))
        else:
            if not static_text.strip():
                logger.warning("Статический парсинг (aiohttp): пустой результат")
            else:
                logger.warning(
                    "Статический парсинг (aiohttp): мало текста (%s символов)",
                    len(static_text),
                )
            logger.warning("Пробую requests-html...")
            try:
                rendered_html = await asyncio.to_thread(self._fetch_rendered_html_sync, url)
                if not (rendered_html or "").strip():
                    logger.warning("requests-html: пустой HTML после рендеринга")
                else:
                    rendered_text = self._extract_text(rendered_html, stage="requests-html")
                    if not rendered_text.strip():
                        logger.warning("requests-html: текст не извлечён из отрендеренной страницы")
                    elif self._has_enough_text(rendered_text):
                        logger.info(
                            "Fallback (requests-html) успешен: %s символов",
                            len(rendered_text),
                        )
                    if not image_url and rendered_html:
                        image_url = await self._extract_og_image(rendered_html, url)
                    text = self._combine_texts(static_text, rendered_text)
            except Exception as exc:  # noqa: BLE001
                logger.warning("requests-html fallback не сработал для %s: %s", url, exc)

            if not self._has_enough_text(text):
                logger.error(
                    "Не удалось извлечь достаточно текста: статика %s символов, "
                    "requests-html %s символов",
                    len(static_text),
                    len(rendered_text),
                )

        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]

        if not text:
            logger.error("❌ Ошибка парсинга %s: пустой контент", url)
            raise RuntimeError("Страница не содержит текста")

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
                    if self._looks_like_beget_cookie_challenge(html):
                        logger.warning(
                            "Страница вернула Beget JS-cookie challenge; повторяю запрос с cookie",
                        )
                        return await self._fetch_with_beget_cookie(session, url)
                    return html

        except asyncio.TimeoutError:
            logger.error("❌ Ошибка парсинга %s: таймаут соединения", url)
            raise RuntimeError("Таймаут при загрузке страницы") from None
        except aiohttp.ClientError as exc:
            logger.error("❌ Ошибка парсинга %s: %s", url, exc)
            raise RuntimeError(f"Сетевая ошибка: {exc}") from exc

    @staticmethod
    def _looks_like_beget_cookie_challenge(html: str) -> bool:
        """Распознать короткую JS-страницу Beget, которая ставит cookie и reload."""
        low = (html or "").lower()
        return (
            "document.cookie" in low
            and "beget=begetok" in low
            and "location.reload" in low
        )

    async def _fetch_with_beget_cookie(self, session: aiohttp.ClientSession, url: str) -> str:
        """Повторить загрузку страниц Beget после JS-cookie challenge."""
        headers = dict(self.HEADERS)
        headers["Cookie"] = "beget=begetok"
        async with session.get(url, headers=headers, allow_redirects=True) as response:
            if response.status >= 400:
                logger.error("❌ Ошибка парсинга %s после Beget cookie: HTTP %s", url, response.status)
                raise RuntimeError(f"Ошибка HTTP {response.status} при повторной загрузке страницы")
            html = await response.text(errors="replace")
            size_kb = max(len(html) // 1024, 1)
            logger.info(
                "✅ Страница загружена после Beget cookie (%s), размер: %sKB",
                response.status,
                size_kb,
            )
            return html

    def _fetch_rendered_html_sync(self, url: str) -> str:
        """Синхронно загрузить и отрендерить страницу через requests-html/Chromium."""
        try:
            from requests_html import HTMLSession
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "requests-html не установлен или недоступен; установите зависимости из requirements.txt"
            ) from exc

        headers = dict(self.MOBILE_HEADERS)
        session = HTMLSession()
        try:
            response = session.get(url, headers=headers, timeout=self.TIMEOUT_SECONDS)
            response.raise_for_status()
            response.html.render(timeout=self.RENDER_TIMEOUT_SECONDS)
            rendered = str(response.html.html or "")
            logger.info(
                "requests-html: страница отрендерена (%s символов HTML, timeout=%ss)",
                len(rendered),
                self.RENDER_TIMEOUT_SECONDS,
            )
            return rendered
        finally:
            session.close()

    def _html_fragment_to_text(self, content_html: str) -> str:
        """Извлечь текст из HTML-фрагмента (readability summary или body)."""
        soup = BeautifulSoup(content_html, "lxml")
        for tag in soup(self._TAGS_TO_REMOVE):
            tag.decompose()
        raw_text = soup.get_text(separator="\n")
        return self._clean_extracted_text(self._clean_text(raw_text))

    def _body_text_from_html(self, html: str) -> str:
        """Извлечь весь текст из ``<body>`` — запасной путь, если readability пуст."""
        soup = BeautifulSoup(html, "lxml")
        body = soup.find("body")
        if body is None:
            return self._html_fragment_to_text(html)
        for tag in body(self._TAGS_TO_REMOVE):
            tag.decompose()
        raw_text = body.get_text(separator="\n")
        return self._clean_extracted_text(self._clean_text(raw_text))

    def _extract_structured_recipe_text(self, html: str) -> str:
        """Достать рецепт из JSON-LD или типичных DOM-блоков, не теряя короткие ингредиенты."""
        soup = BeautifulSoup(html, "lxml")
        json_ld_text = self._extract_json_ld_recipe_text(soup)
        if json_ld_text:
            return json_ld_text
        return self._extract_dom_recipe_text(soup)

    def _extract_json_ld_recipe_text(self, soup: BeautifulSoup) -> str:
        decoder = json.JSONDecoder(strict=False)
        for script in soup.find_all("script", type="application/ld+json"):
            raw = (script.get_text() or "").strip()
            if not raw or "Recipe" not in raw:
                continue
            try:
                payload = decoder.decode(raw)
            except json.JSONDecodeError as exc:
                logger.warning("JSON-LD Recipe не распарсен: %s", exc)
                continue
            recipe = self._find_recipe_object(payload)
            if recipe:
                return self._format_recipe_object_text(recipe)
        return ""

    def _find_recipe_object(self, value: Any) -> Optional[dict[str, Any]]:
        if isinstance(value, dict):
            raw_type = value.get("@type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if any(str(t).lower() == "recipe" for t in types):
                return value
            graph = value.get("@graph")
            found = self._find_recipe_object(graph)
            if found:
                return found
            for item in value.values():
                found = self._find_recipe_object(item)
                if found:
                    return found
        if isinstance(value, list):
            for item in value:
                found = self._find_recipe_object(item)
                if found:
                    return found
        return None

    def _format_recipe_object_text(self, recipe: dict[str, Any]) -> str:
        lines: List[str] = []
        title = self._plain_text(recipe.get("name"))
        description = self._plain_text(recipe.get("description"))
        if title:
            lines.append(title)
        if description:
            lines.append(description)
        ingredients = recipe.get("recipeIngredient")
        if isinstance(ingredients, list) and ingredients:
            lines.append("Ингредиенты:")
            for item in ingredients:
                text = self._plain_text(item)
                if text:
                    lines.append(f"Ингредиент: {text}")
        instructions = recipe.get("recipeInstructions")
        steps = self._recipe_instruction_texts(instructions)
        if steps:
            lines.append("Приготовление:")
            for index, step in enumerate(steps, 1):
                lines.append(f"Шаг {index}: {step}")
        return "\n".join(lines).strip()

    def _recipe_instruction_texts(self, instructions: Any) -> List[str]:
        if isinstance(instructions, str):
            text = self._plain_text(instructions)
            return [text] if text else []
        if not isinstance(instructions, list):
            return []
        steps: List[str] = []
        for item in instructions:
            if isinstance(item, dict):
                text = self._plain_text(item.get("text") or item.get("name"))
            else:
                text = self._plain_text(item)
            if text:
                steps.append(text)
        return steps

    def _extract_dom_recipe_text(self, soup: BeautifulSoup) -> str:
        root = soup.select_one("article.post-recipe") or soup
        lines: List[str] = []
        title = soup.find("h1")
        if title:
            title_text = self._plain_text(title.get_text(" ", strip=True))
            if title_text:
                lines.append(title_text)
        ingredients: List[str] = []
        for item in root.select(".recipe-ingredients li"):
            name_el = item.select_one(".name")
            value_el = item.select_one(".value")
            name = self._plain_text(name_el.get_text(" ", strip=True) if name_el else "")
            value = self._plain_text(value_el.get_text(" ", strip=True) if value_el else "")
            text = " ".join(part for part in (value, name) if part).strip()
            if text:
                ingredients.append(text)
        if ingredients:
            lines.append("Ингредиенты:")
            lines.extend(f"Ингредиент: {item}" for item in ingredients)
        steps: List[str] = []
        for item in root.select(".recipe-cooking li"):
            text_el = item.select_one(".recipe-cooking__text") or item
            text = self._plain_text(text_el.get_text(" ", strip=True))
            if text:
                steps.append(text)
        if steps:
            lines.append("Приготовление:")
            lines.extend(f"Шаг {index}: {step}" for index, step in enumerate(steps, 1))
        return "\n".join(lines).strip()

    @staticmethod
    def _plain_text(value: Any) -> str:
        if value is None:
            return ""
        text = BeautifulSoup(str(value), "lxml").get_text(" ", strip=True)
        return re.sub(r"\s+", " ", text).strip()

    def _extract_text(self, html: str, *, stage: str = "static") -> str:
        if not (html or "").strip():
            logger.warning("%s: HTML пустой, текст не извлечён", stage)
            return ""

        readability_text = ""
        if _ReadabilityDocument is not None:
            try:
                doc = _ReadabilityDocument(html)
                summary = doc.summary(html_partial=True)
                if summary and summary.strip():
                    readability_text = self._html_fragment_to_text(summary)
                else:
                    logger.warning("readability (%s): пустой summary", stage)
            except Exception as exc:  # noqa: BLE001
                logger.warning("readability (%s): ошибка — %s", stage, exc)
        else:
            logger.warning("readability (%s): модуль недоступен", stage)

        structured_text = self._extract_structured_recipe_text(html)
        body_text = self._body_text_from_html(html)
        if structured_text:
            logger.info(
                "Структурированный рецепт (%s): извлечено %s символов",
                stage,
                len(structured_text),
            )
            body_text = self._combine_texts(structured_text, body_text)
            if readability_text:
                readability_text = self._combine_texts(structured_text, readability_text)

        if not readability_text.strip():
            logger.warning(
                "readability не дал текста, использую body: %s символов",
                len(body_text),
            )
            return body_text

        if len(readability_text) < self.MIN_USEFUL_TEXT_LENGTH:
            logger.warning(
                "readability (%s): мало текста (%s симв.), использую body: %s символов",
                stage,
                len(readability_text),
                len(body_text),
            )
            return body_text or readability_text

        logger.info("readability (%s): успешно, %s символов", stage, len(readability_text))
        return readability_text

    def _has_enough_text(self, text: str) -> bool:
        cleaned = (text or "").strip()
        return (
            len(cleaned) >= self.MIN_USEFUL_TEXT_LENGTH
            and bool(self._RECIPE_KEYWORDS_RE.search(cleaned))
        )

    def _combine_texts(self, first: str, second: str) -> str:
        first = (first or "").strip()
        second = (second or "").strip()
        if not first:
            return second
        if not second:
            return first
        if first in second:
            return second
        if second in first:
            return first
        return self._clean_extracted_text(f"{first}\n\n{second}")

    def _clean_extracted_text(self, text: str) -> str:
        """Очистить текст рецепта от коротких навигационных и служебных строк."""
        lines = text.splitlines()
        cleaned_lines: List[str] = []
        for line in lines:
            collapsed = re.sub(r"[ \t\u00a0]+", " ", line).strip()
            if not collapsed:
                continue
            if len(collapsed) < 20:
                continue
            if self._TEXT_STOP_WORDS_RE.search(collapsed):
                continue
            cleaned_lines.append(collapsed)
        cleaned = "\n".join(cleaned_lines)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

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
APIFY_TIKTOK_TRANSCRIPT_ACTOR = "scrape-creators~best-tiktok-transcripts-scraper"
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


class YouTubeParser(YtdlpWhisperMixin, BaseParser):
    """YouTube: yt-dlp метаданные + Whisper + captionTracks / Apify-fallback.

    Пайплайн совпадает с Instagram/TikTok: описание в приоритете, речь — дополнение.
    YouTube Data API не требуется — метаданные берутся через yt-dlp.
    """

    source_type: str = "youtube"
    generate_image_default: bool = False
    PLATFORM_NAME: str = "YouTube"
    AUDIO_OUTTMPL: str = "/tmp/youtube_audio_%(id)s.%(ext)s"
    AUDIO_LOG_LABEL: str = "YouTube"
    HEADERS: dict = WebParser.HEADERS

    def __init__(self, apify_api_token: str = "") -> None:
        self._apify_api_token = (apify_api_token or "").strip()
        self._init_ytdlp_whisper()

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

    def _metadata_from_info(self, info: Optional[dict]) -> tuple[str, str, str]:
        return metadata_from_ytdlp_info(
            info,
            prepend_uploader=True,
            title_cleaner=_strip_youtube_title_text,
            description_cleaner=_strip_youtube_description_text,
        )

    async def parse(
        self,
        url: str,
        *,
        on_progress: Optional[ProgressCallback] = None,
    ) -> ParseResult:
        if not self.can_parse(url):
            raise ValueError(f"YouTubeParser не поддерживает URL: {url!r}")
        return await self.parse_ytdlp_video(
            url,
            on_progress=on_progress,
            supplement_transcript=self._supplement_youtube_transcript,
        )

    def _supplement_youtube_transcript(self, url: str) -> str:
        """Бесплатные captionTracks, если Whisper не дал текста."""
        video_id = self._extract_youtube_video_id(url)
        if not video_id:
            return ""
        return self._fetch_subtitles_public(video_id)

    @staticmethod
    def _resolve_audio_path(info: Optional[dict]) -> str:
        info = info or {}
        requested = info.get("requested_downloads")
        if isinstance(requested, list) and requested:
            first = requested[0]
            if isinstance(first, dict):
                fp = first.get("filepath") or first.get("_filename") or ""
                if fp:
                    base, _ext = os.path.splitext(fp)
                    return base + ".mp3"
        video_id = str(info.get("id") or "").strip()
        if video_id:
            return f"/tmp/youtube_audio_{video_id}.mp3"
        return ""

    @staticmethod
    def _extract_balanced_json(text: str, marker: str) -> str:
        """Извлечь JSON-объект после JS-маркера вроде ``ytInitialPlayerResponse``."""
        idx = text.find(marker)
        if idx < 0:
            return ""
        start = text.find("{", idx)
        if start < 0:
            return ""
        depth = 0
        in_string = False
        escape = False
        for pos in range(start, len(text)):
            ch = text[pos]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start: pos + 1]
        return ""

    @staticmethod
    def _choose_caption_track(tracks: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        """Выбрать дорожку: ru manual → ru auto → en manual → en auto → first."""
        if not tracks:
            return None

        def lang(track: dict[str, Any]) -> str:
            return str(track.get("languageCode") or "").lower()

        def is_auto(track: dict[str, Any]) -> bool:
            return str(track.get("kind") or "").lower() == "asr"

        priorities = [
            lambda t: lang(t).startswith("ru") and not is_auto(t),
            lambda t: lang(t).startswith("ru"),
            lambda t: lang(t).startswith("en") and not is_auto(t),
            lambda t: lang(t).startswith("en"),
            lambda t: not is_auto(t),
            lambda _t: True,
        ]
        for predicate in priorities:
            for track in tracks:
                if isinstance(track, dict) and predicate(track):
                    return track
        return None

    def _fetch_subtitles_public(self, video_id: str) -> str:
        """Бесплатно получить публичные субтитры из YouTube captionTracks, если доступны."""
        watch_url = f"https://www.youtube.com/watch?v={video_id}"
        headers = {
            "User-Agent": WebParser.HEADERS["User-Agent"],
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        try:
            req = urllib.request.Request(watch_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                watch_html = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.info("YouTube public captions: не удалось загрузить watch page: %s", exc)
            return ""

        raw_player = self._extract_balanced_json(watch_html, "ytInitialPlayerResponse")
        if not raw_player:
            logger.info("YouTube public captions: ytInitialPlayerResponse не найден")
            return ""
        try:
            player = json.loads(raw_player)
        except json.JSONDecodeError as exc:
            logger.info("YouTube public captions: player JSON не распарсен: %s", exc)
            return ""

        captions = player.get("captions") if isinstance(player, dict) else None
        tracklist = (
            captions.get("playerCaptionsTracklistRenderer")
            if isinstance(captions, dict)
            else None
        )
        tracks = tracklist.get("captionTracks") if isinstance(tracklist, dict) else None
        if not isinstance(tracks, list) or not tracks:
            logger.info("YouTube public captions: публичные captionTracks не найдены")
            return ""

        track = self._choose_caption_track(tracks)
        if not track:
            return ""
        base_url = str(track.get("baseUrl") or "").strip()
        if not base_url:
            return ""
        name = track.get("name")
        lang_label = name.get("simpleText") if isinstance(name, dict) else ""
        sep = "&" if "?" in base_url else "?"
        transcript_url = base_url if "fmt=" in base_url else f"{base_url}{sep}fmt=srv3"
        try:
            req = urllib.request.Request(transcript_url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw_xml = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.info("YouTube public captions: не удалось скачать transcript: %s", exc)
            return ""
        if not raw_xml.strip():
            logger.info("YouTube public captions: transcript пустой")
            return ""

        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as exc:
            logger.info("YouTube public captions: transcript XML не распарсен: %s", exc)
            return ""

        parts: List[str] = []
        for elem in root.iter():
            if elem.tag.endswith("text") or elem.tag.endswith("p"):
                text = html_lib.unescape("".join(elem.itertext()))
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    parts.append(text)
        collapsed = _apify_collapse_subtitle_text(parts)
        if collapsed:
            logger.info(
                "Получены публичные субтитры YouTube captionTracks (%s символов, %s)",
                len(collapsed),
                lang_label or str(track.get("languageCode") or "unknown"),
            )
        return collapsed

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

    def _fetch_apify_fallback(self, url: str) -> tuple[str, str, str, str]:
        video_id = self._extract_youtube_video_id(url)
        transcript = self._fetch_subtitles_apify(video_id) if video_id else ""
        return "", "", transcript, ""


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


def _instagram_compose_text(title: str, description: str, transcript: str) -> str:
    return video_compose_text("Reels", title, description, transcript)


class InstagramParser(YtdlpWhisperMixin, BaseParser):
    """Instagram Reels: yt-dlp + Whisper + Apify-fallback (``apple_yang~instagram-transcripts-scraper``)."""

    source_type: str = "instagram"
    generate_image_default: bool = False
    PLATFORM_NAME: str = "Reels"
    AUDIO_OUTTMPL: str = "/tmp/reels_audio_%(id)s.%(ext)s"
    AUDIO_LOG_LABEL: str = "Instagram Reels"
    HEADERS: dict = WebParser.HEADERS
    COOKIE_FILE_PATH: str = "/tmp/instagram_cookies.txt"

    def __init__(self, apify_api_token: str = "", instagram_session_id: str = "") -> None:
        self._apify_api_token = (apify_api_token or "").strip()
        self.sessionid = (instagram_session_id or "").strip()
        self._init_ytdlp_whisper()

    def _ytdlp_cookiefile(self) -> str:
        return self._create_cookie_file()

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

    async def parse(
        self,
        url: str,
        *,
        on_progress: Optional[ProgressCallback] = None,
    ) -> ParseResult:
        if not self.can_parse(url):
            raise ValueError(f"InstagramParser не поддерживает URL: {url!r}")
        return await self.parse_ytdlp_video(url, on_progress=on_progress)

    @staticmethod
    def _resolve_audio_path(info: Optional[dict]) -> str:
        info = info or {}
        requested = info.get("requested_downloads")
        if isinstance(requested, list) and requested:
            first = requested[0]
            if isinstance(first, dict):
                fp = first.get("filepath") or first.get("_filename") or ""
                if fp:
                    base, _ext = os.path.splitext(fp)
                    return base + ".mp3"
        video_id = str(info.get("id") or "").strip()
        if video_id:
            return f"/tmp/reels_audio_{video_id}.mp3"
        return ""

    def _create_cookie_file(self) -> str:
        if not self.sessionid:
            return ""
        cookie_line = "\t".join(
            [".instagram.com", "TRUE", "/", "TRUE", "0", "sessionid", self.sessionid]
        )
        content = "# Netscape HTTP Cookie File\n" + cookie_line + "\n"
        try:
            with open(self.COOKIE_FILE_PATH, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            logger.warning("Не удалось создать cookie-файл Instagram: %s", exc)
            return ""
        return self.COOKIE_FILE_PATH

    def _fetch_apify_fallback(self, url: str) -> tuple[str, str, str, str]:
        token = (self._apify_api_token or "").strip()
        if not token:
            logger.error("Ошибка получения Instagram субтитров")
            return "", "", "", ""

        run_url = (
            f"https://api.apify.com/v2/acts/{APIFY_INSTAGRAM_TRANSCRIPT_ACTOR}/runs"
            f"?waitForFinish={APIFY_INSTAGRAM_WAIT_FINISH_SEC}"
        )
        body: dict[str, Any] = {"videoUrl": url}
        if self.sessionid:
            logger.info("Используется Instagram sessionid")
            body["sessionid"] = self.sessionid
        envelope = _apify_http_json("POST", run_url, token, body)
        if not isinstance(envelope, dict):
            return "", "", "", ""

        run = envelope.get("data")
        if not isinstance(run, dict):
            return "", "", "", ""
        run_id = run.get("id")
        if not run_id:
            return "", "", "", ""

        items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?format=json"
        items_payload = _apify_http_json("GET", items_url, token, None)
        item: Any = items_payload
        if isinstance(items_payload, list):
            item = items_payload[0] if items_payload else None
        title, description, transcript_text, img_url = _instagram_extract_from_item(item)
        if title or description or transcript_text:
            logger.info("Получены данные через Apify Instagram Transcripts Scraper")
        return title, description, transcript_text, img_url

    def _fetch_via_apify(self, url: str) -> tuple[str, str, str, str]:
        """Совместимость с тестами: алиас :meth:`_fetch_apify_fallback`."""
        return self._fetch_apify_fallback(url)


def _tiktok_extract_from_item(item: Any) -> tuple[str, str, str, str]:
    if not isinstance(item, dict):
        return "", "", "", ""
    title = safe_video_str(item.get("title") or item.get("videoTitle"))
    description = safe_video_str(
        item.get("description") or item.get("caption") or item.get("videoDescription")
    )
    transcript = safe_video_str(
        item.get("transcript")
        or item.get("transcriptText")
        or item.get("subtitle")
        or item.get("text")
    )
    if not transcript:
        transcript = _instagram_segments_to_text(item.get("segments"))
    img_url = safe_video_str(
        item.get("thumbnail") or item.get("cover") or item.get("img") or item.get("imageUrl")
    )
    return title, description, transcript, img_url


class TikTokParser(YtdlpWhisperMixin, BaseParser):
    """TikTok: yt-dlp метаданные + Whisper + Apify-fallback."""

    source_type: str = "tiktok"
    generate_image_default: bool = False
    PLATFORM_NAME: str = "TikTok"
    AUDIO_OUTTMPL: str = "/tmp/tiktok_audio_%(id)s.%(ext)s"
    AUDIO_LOG_LABEL: str = "TikTok"
    HEADERS: dict = WebParser.HEADERS

    def __init__(self, apify_api_token: str = "") -> None:
        self._apify_api_token = (apify_api_token or "").strip()
        self._init_ytdlp_whisper()

    @staticmethod
    def can_parse(url: str) -> bool:
        if not isinstance(url, str) or not url.strip():
            return False
        u = url.lower().strip()
        return bool(
            re.search(
                r"(?:^|://)(?:www\.|vm\.|vt\.)?tiktok\.com/",
                u,
                re.IGNORECASE,
            )
        )

    async def parse(
        self,
        url: str,
        *,
        on_progress: Optional[ProgressCallback] = None,
    ) -> ParseResult:
        if not self.can_parse(url):
            raise ValueError(f"TikTokParser не поддерживает URL: {url!r}")
        return await self.parse_ytdlp_video(url, on_progress=on_progress)

    @staticmethod
    def _resolve_audio_path(info: Optional[dict]) -> str:
        info = info or {}
        requested = info.get("requested_downloads")
        if isinstance(requested, list) and requested:
            first = requested[0]
            if isinstance(first, dict):
                fp = first.get("filepath") or first.get("_filename") or ""
                if fp:
                    base, _ext = os.path.splitext(fp)
                    return base + ".mp3"
        video_id = str(info.get("id") or "").strip()
        if video_id:
            return f"/tmp/tiktok_audio_{video_id}.mp3"
        return ""

    def _fetch_apify_fallback(self, url: str) -> tuple[str, str, str, str]:
        token = (self._apify_api_token or "").strip()
        if not token:
            logger.warning("TikTok Apify: токен не задан")
            return "", "", "", ""

        run_url = (
            f"https://api.apify.com/v2/acts/{APIFY_TIKTOK_TRANSCRIPT_ACTOR}/runs"
            f"?waitForFinish={APIFY_WAIT_FINISH_SEC}"
        )
        envelope = _apify_http_json("POST", run_url, token, {"videos": [url]})
        if not isinstance(envelope, dict):
            return "", "", "", ""

        run = envelope.get("data")
        if not isinstance(run, dict):
            return "", "", "", ""
        run_id = run.get("id")
        if not run_id:
            return "", "", "", ""

        items_url = f"https://api.apify.com/v2/actor-runs/{run_id}/dataset/items?format=json"
        items_payload = _apify_http_json("GET", items_url, token, None)
        item: Any = items_payload
        if isinstance(items_payload, list):
            item = items_payload[0] if items_payload else None
        title, description, transcript, img_url = _tiktok_extract_from_item(item)
        if title or description or transcript:
            logger.info("Получены данные через Apify TikTok Transcripts Scraper")
        return title, description, transcript, img_url




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

    async def parse(
        self,
        url: str,
        *,
        on_progress: Optional[ProgressCallback] = None,
    ) -> ParseResult:
        parser = self.get_parser(url)
        if parser is None:
            raise ValueError(f"Не удалось найти парсер для URL: {url}")
        logger.info("🔍 Парсинг через %s: %s", parser.source_type, url[:80])
        if isinstance(parser, YtdlpWhisperMixin):
            return await parser.parse(url, on_progress=on_progress)
        return await parser.parse(url)

    @property
    def parsers(self) -> List[BaseParser]:
        return list(self._parsers)


def create_parser_registry(cfg: Optional[object] = None) -> ParserRegistry:
    """Реестр: YouTube, TikTok, Instagram, Web."""
    if cfg is None:
        from config import config as _cfg
        cfg = _cfg
    apify_tok = str(getattr(cfg, "apify_api_token", "") or "")
    instagram_session_id = str(getattr(cfg, "instagram_session_id", "") or "")
    registry = ParserRegistry()
    registry.register(YouTubeParser(apify_api_token=apify_tok))
    registry.register(TikTokParser(apify_api_token=apify_tok))
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
        assert YouTubeParser._extract_youtube_video_id(
            "https://youtu.be/7oUqRIysbag?si=d9i2N7NW1IM6-WB3",
        ) == "7oUqRIysbag"
        raw_player = 'x; ytInitialPlayerResponse = {"a":{"b":1},"c":"} ok"}; y;'
        assert json.loads(YouTubeParser._extract_balanced_json(raw_player, "ytInitialPlayerResponse"))["a"]["b"] == 1
        chosen_track = YouTubeParser._choose_caption_track([
            {"languageCode": "en", "kind": "asr", "baseUrl": "en-auto"},
            {"languageCode": "ru", "baseUrl": "ru-manual"},
        ])
        assert chosen_track and chosen_track["baseUrl"] == "ru-manual"

        class _MockUrlopenResponse:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def __enter__(self) -> "_MockUrlopenResponse":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def read(self) -> bytes:
                return self._body

        player_json = json.dumps({
            "captions": {
                "playerCaptionsTracklistRenderer": {
                    "captionTracks": [
                        {
                            "languageCode": "ru",
                            "baseUrl": "https://youtube.test/timedtext?v=abcdefghijk",
                            "name": {"simpleText": "Русский"},
                        }
                    ]
                }
            }
        })
        watch_html = f"<script>var ytInitialPlayerResponse = {player_json};</script>"
        transcript_xml = "<transcript><text>Первый сегмент</text><text>второй сегмент</text></transcript>"
        with patch("urllib.request.urlopen", side_effect=[
            _MockUrlopenResponse(watch_html.encode("utf-8")),
            _MockUrlopenResponse(transcript_xml.encode("utf-8")),
        ]):
            public_subs = YouTubeParser()._fetch_subtitles_public("abcdefghijk")
        assert public_subs == "Первый сегмент второй сегмент"
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
        assert YouTubeParser.generate_image_default is False
        assert TikTokParser.generate_image_default is False
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

        beget_challenge = (
            "<html><head><script>document.cookie='beget=begetok; path=/';"
            "location.reload();</script></head><body></body></html>"
        )
        assert WebParser._looks_like_beget_cookie_challenge(beget_challenge)
        assert not WebParser._looks_like_beget_cookie_challenge("<html><body>Рецепт</body></html>")
        print("✅ WebParser Beget cookie challenge")

        wp_clean = WebParser()
        dirty_text = (
            "Войти\n"
            "Коротко\n"
            "Ингредиенты для домашнего пирога: мука, яйца и сахар\n\n\n\n"
            "Поделиться рецептом\n"
            "Приготовление занимает около сорока минут"
        )
        cleaned_text = wp_clean._clean_extracted_text(dirty_text)
        assert "Войти" not in cleaned_text
        assert "Коротко" not in cleaned_text
        assert "\n\n\n" not in cleaned_text
        assert "Ингредиенты" in cleaned_text
        print("✅ WebParser очистка извлечённого текста")

        wp_body = WebParser()
        onetable_recipe = (
            "Желе из йогурта и желатина с фруктами — рецепт для всей семьи. "
            "Ингредиенты: йогурт натуральный, желатин, сахар, фрукты по вкусу. "
            + "подробное описание ингредиентов " * 35
            + "\nПриготовление: замочите желатин, смешайте с йогуртом и фруктами. "
            + "рецепт простой и быстрый " * 35
        )
        onetable_json_ld = json.dumps({
            "@context": "https://schema.org/",
            "@type": "Recipe",
            "name": "Желе из йогурта и желатина",
            "recipeIngredient": [
                "400 г Греческий йогурт",
                "10 г Желатин",
                "60 мл Вода",
            ],
            "recipeInstructions": [
                {"@type": "HowToStep", "text": "Подготовьте продукты."},
                {"@type": "HowToStep", "text": "Смешайте йогурт с желатином."},
            ],
        }, ensure_ascii=False)
        onetable_html = (
            f"<html><head><title>Желе</title>"
            f"<meta property=\"og:image\" content=\"/images/zhele.jpg\">"
            f"<script type=\"application/ld+json\">{onetable_json_ld}</script></head><body>"
            f"<script>window.__noise = true;</script><style>.hidden{{display:none}}</style>"
            f"<nav>Войти на сайт onetable</nav>"
            f"<main><article><h1>Желе из йогурта</h1><p>{onetable_recipe}</p></article></main>"
            f"</body></html>"
        )

        class _EmptyReadabilityDoc:
            def __init__(self, html: str) -> None:
                self._html = html

            def summary(self, html_partial: bool = True) -> str:
                return "<div></div>"

        with patch.object(mod, "_ReadabilityDocument", _EmptyReadabilityDoc):
            body_fallback_text = wp_body._extract_text(onetable_html, stage="test (body fallback)")
        assert "Ингредиенты" in body_fallback_text
        assert "Ингредиент: 10 г Желатин" in body_fallback_text
        assert "Приготовление" in body_fallback_text
        assert "Шаг 1: Подготовьте продукты." in body_fallback_text
        assert "window.__noise" not in body_fallback_text
        assert len(body_fallback_text) >= WebParser.MIN_USEFUL_TEXT_LENGTH
        print("✅ WebParser body fallback при пустом readability (onetable-мок)")

        static_html = "<html><body><p>Слишком короткое описание без полной структуры</p></body></html>"
        rendered_recipe = (
            "Ингредиенты для тестового рецепта: "
            + "мука сахар масло яйца молоко " * 60
            + "\nПриготовление: смешайте ингредиенты, выпекайте до готовности. "
            + "рецепт подходит для проверки fallback "
            + "подробное описание " * 40
        )
        rendered_html = f"<html><body><article><p>{rendered_recipe}</p></article></body></html>"
        wp_fallback = WebParser()
        with patch.object(WebParser, "_fetch", new_callable=AsyncMock, return_value=static_html), patch.object(
            WebParser,
            "_extract_og_image",
            new_callable=AsyncMock,
            return_value=None,
        ), patch.object(
            WebParser,
            "_fetch_rendered_html_sync",
            return_value=rendered_html,
        ) as mock_render:
            fallback_result = await wp_fallback.parse("https://example.test/js-recipe")
        mock_render.assert_called_once()
        assert "Ингредиенты" in fallback_result.text
        assert len(fallback_result.text) >= WebParser.MIN_USEFUL_TEXT_LENGTH
        print("✅ WebParser fallback requests-html (мок)")

        wp_onetable = WebParser()
        td_onetable = tempfile.mkdtemp()

        async def _fake_onetable_image_download(_u: str, dest: str) -> bool:
            with open(dest, "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")
            return True

        with patch.object(WebParser, "_fetch", new_callable=AsyncMock, return_value=static_html), patch.object(
            WebParser,
            "_fetch_rendered_html_sync",
            return_value=onetable_html,
        ), patch.object(
            WebParser,
            "_download_binary_to_path",
            side_effect=_fake_onetable_image_download,
        ), patch.object(mod, "_ReadabilityDocument", _EmptyReadabilityDoc), patch.object(mod, "IMAGES_DIR", td_onetable):
            onetable_result = await wp_onetable.parse(
                "https://onetable.ru/zhele-iz-jogurta-i-zhelatina-s-fruktami/",
            )
        assert "Ингредиенты" in onetable_result.text
        assert "Ингредиент: 400 г Греческий йогурт" in onetable_result.text
        assert "Шаг 2: Смешайте йогурт с желатином." in onetable_result.text
        assert len(onetable_result.text) >= WebParser.MIN_USEFUL_TEXT_LENGTH
        assert wp_onetable.last_image_url == "https://onetable.ru/images/zhele.jpg"
        assert onetable_result.image_url is not None and os.path.isfile(onetable_result.image_url)
        print("✅ WebParser onetable URL + body fallback + og:image (мок)")

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
            yt_dummy = YouTubeParser(apify_api_token="")
            rd2 = {"raw_text": "Плов узбекский", "image_url": None}
            with patch.object(YouTubeParser, "generate_image_default", True):
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

        thumb_info = {
            "thumbnails": [
                {"url": "https://cdn.example.test/small.jpg", "width": 150, "height": 150},
                {"url": "https://cdn.example.test/cover_hd.jpg", "width": 1080, "height": 1920},
            ],
            "thumbnail": "https://cdn.example.test/fallback.jpg",
        }
        assert pick_best_thumbnail(thumb_info) == "https://cdn.example.test/cover_hd.jpg"
        assert pick_best_thumbnail({"thumbnail": "https://only.jpg"}) == "https://only.jpg"
        meta_title, meta_desc, meta_thumb = metadata_from_ytdlp_info({
            "title": "Reel Title",
            "description": "Полный рецепт борща с говядиной",
            "uploader": "chef_maria",
            "thumbnails": thumb_info["thumbnails"],
        })
        assert meta_title == "Reel Title"
        assert "борщ" in meta_desc and "@chef_maria" in meta_desc
        assert meta_thumb == "https://cdn.example.test/cover_hd.jpg"
        composed = _instagram_compose_text("T", "Описание", "Речь")
        assert "приоритетный источник" in composed and "дополнение" in composed
        print("✅ Instagram thumbnail metadata + compose helpers")

        _ig_metadata_empty: dict[str, Any] = {}

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

        forced_img = os.path.join(td_ig, "ig_cover.jpg")
        with open(forced_img, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xd9")

        # parse() через Apify-fallback: локальное скачивание принудительно
        # роняем, чтобы проверить именно ветку Apify + скачивание картинки.
        with patch.object(
            InstagramParser,
            "_fetch_ytdlp_info",
            new_callable=AsyncMock,
            return_value=_ig_metadata_empty,
        ), patch.object(
            InstagramParser,
            "_download_audio",
            new_callable=AsyncMock,
            side_effect=RuntimeError("local disabled in test"),
        ), patch.object(
            InstagramParser,
            "_fetch_apify_fallback",
            return_value=(
                "IG Title",
                "Описание и ингредиенты",
                "текст субтитров",
                "https://cdn.example.test/recipe.jpg",
            ),
        ), patch.object(
            InstagramParser,
            "_download_thumbnail",
            new_callable=AsyncMock,
            return_value=forced_img,
        ), patch.object(mod, "IMAGES_DIR", td_ig):
            ig_apify = InstagramParser(apify_api_token="dummy")
            ig_apify._local_enabled = True
            ig_out = await ig_apify.parse(
                "https://www.instagram.com/reel/xyz123xxxxx/",
            )
        assert "IG Title" in ig_out.text and "Описание" in ig_out.text and "субтитров" in ig_out.text
        assert ig_out.image_url is not None and ig_out.image_url.startswith(td_ig)
        assert os.path.isfile(ig_out.image_url)
        no_img = await InstagramParser(apify_api_token="dummy")._download_thumbnail("")
        assert no_img is None
        print("✅ Instagram parse() с Apify-fallback (локальный путь отключён)")

        # --- Описание + Whisper объединяются (ингредиенты в caption, шаги в видео) #
        _ig_recipe_caption = (
            "Ингредиенты:\n"
            "• фарш 500 г\n"
            "• лук 1 шт\n"
            "• яйцо 1 шт"
        )
        forced_combo_img = os.path.join(td_ig, "combo_cover.jpg")
        with open(forced_combo_img, "wb") as fh:
            fh.write(b"\xff\xd8\xff\xd9")
        with patch.object(
            InstagramParser,
            "_fetch_ytdlp_info",
            new_callable=AsyncMock,
            return_value={
                "title": "Котлеты в духовке",
                "description": _ig_recipe_caption,
                "thumbnails": thumb_info["thumbnails"],
            },
        ), patch.object(
            InstagramParser,
            "_download_audio",
            new_callable=AsyncMock,
            return_value="/tmp/reels_audio_caption.mp3",
        ) as mock_dl_combo, patch.object(
            InstagramParser,
            "_transcribe_audio",
            new_callable=AsyncMock,
            return_value="Смешать фарш с луком, сформировать котлеты и запекать 40 минут.",
        ) as mock_tr_combo, patch.object(
            InstagramParser,
            "_download_thumbnail",
            new_callable=AsyncMock,
            return_value=forced_combo_img,
        ), patch.object(mod, "IMAGES_DIR", td_ig):
            ig_combo = InstagramParser(apify_api_token="dummy")
            ig_combo._local_enabled = True
            combo_out = await ig_combo.parse(
                "https://www.instagram.com/reel/fullcaption123/",
            )
        mock_dl_combo.assert_awaited_once()
        mock_tr_combo.assert_awaited_once()
        assert "Описание (Reels, приоритетный источник)" in combo_out.text
        assert "фарш 500" in combo_out.text
        assert "Речь в видео" in combo_out.text and "запекать" in combo_out.text
        assert combo_out.image_url is not None and combo_out.image_url.startswith(td_ig)
        print("✅ Instagram: описание + Whisper объединяются + обложка yt-dlp")

        # --- Локальное распознавание (yt-dlp + Whisper), happy path ------- #
        with patch.object(
            InstagramParser,
            "_fetch_ytdlp_info",
            new_callable=AsyncMock,
            return_value={"title": "", "description": "короткий пост без рецепта"},
        ), patch.object(
            InstagramParser,
            "_download_audio",
            new_callable=AsyncMock,
            return_value="/tmp/reels_audio_localtest.mp3",
        ) as mock_dl, patch.object(
            InstagramParser,
            "_transcribe_audio",
            new_callable=AsyncMock,
            return_value="Локальный рецепт из Whisper: смешать муку, сахар и яйца.",
        ) as mock_tr, patch.object(
            InstagramParser,
            "_fetch_apify_fallback",
        ) as mock_apify_unused:
            ig_local = InstagramParser(apify_api_token="dummy")
            ig_local._local_enabled = True  # тест не зависит от наличия ffmpeg
            local_out = await ig_local.parse(
                "https://www.instagram.com/reel/local123abc/",
            )
        mock_dl.assert_awaited_once()
        assert mock_tr.await_args is not None
        assert mock_tr.await_args.args[0] == "/tmp/reels_audio_localtest.mp3"
        mock_apify_unused.assert_not_called()
        assert "Whisper" in local_out.text and "муку" in local_out.text
        assert local_out.image_url is None
        print("✅ Instagram локальное распознавание (мок yt-dlp + Whisper)")

        # --- Пустой транскрипт → fallback на Apify ------------------------ #
        with patch.object(
            InstagramParser,
            "_fetch_ytdlp_info",
            new_callable=AsyncMock,
            return_value=_ig_metadata_empty,
        ), patch.object(
            InstagramParser,
            "_download_audio",
            new_callable=AsyncMock,
            return_value="/tmp/reels_audio_empty.mp3",
        ), patch.object(
            InstagramParser,
            "_transcribe_audio",
            new_callable=AsyncMock,
            return_value="   ",
        ), patch.object(
            InstagramParser,
            "_fetch_apify_fallback",
            return_value=("Apify T", "Apify D", "Apify Tr", ""),
        ) as mock_apify_used:
            ig_empty = InstagramParser(apify_api_token="dummy")
            ig_empty._local_enabled = True  # тест не зависит от наличия ffmpeg
            empty_out = await ig_empty.parse(
                "https://www.instagram.com/reel/empty123abc/",
            )
        assert mock_apify_used.call_count >= 1
        assert "Apify T" in empty_out.text and "Apify Tr" in empty_out.text
        print("✅ Instagram fallback на Apify при пустом транскрипте")

        # --- Таймаут распознавания → fallback на Apify -------------------- #
        async def _slow_transcribe(_path: str, **kwargs: Any) -> str:
            await asyncio.sleep(5)
            return "late"

        with patch.object(
            InstagramParser,
            "_fetch_ytdlp_info",
            new_callable=AsyncMock,
            return_value=_ig_metadata_empty,
        ), patch.object(
            InstagramParser,
            "_download_audio",
            new_callable=AsyncMock,
            return_value="/tmp/reels_audio_slow.mp3",
        ), patch.object(
            InstagramParser,
            "_transcribe_audio",
            side_effect=_slow_transcribe,
        ), patch.object(InstagramParser, "TRANSCRIBE_TIMEOUT_SECONDS", 0.05), patch.object(
            InstagramParser,
            "_fetch_apify_fallback",
            return_value=("Timeout T", "", "Timeout Tr", ""),
        ) as mock_apify_timeout:
            ig_timeout = InstagramParser(apify_api_token="dummy")
            ig_timeout._local_enabled = True
            timeout_out = await ig_timeout.parse(
                "https://www.instagram.com/reel/timeout123abc/",
            )
        assert mock_apify_timeout.call_count >= 1
        assert "Timeout T" in timeout_out.text
        print("✅ Instagram fallback на Apify при таймауте Whisper")

        # --- _create_cookie_file: Netscape-формат с sessionid ------------- #
        td_cookie = tempfile.mkdtemp()
        cookie_target = os.path.join(td_cookie, "instagram_cookies.txt")
        with patch.object(InstagramParser, "COOKIE_FILE_PATH", cookie_target):
            ig_cookie_parser = InstagramParser(
                apify_api_token="dummy",
                instagram_session_id="SESSION_XYZ",
            )
            cookie_path = ig_cookie_parser._create_cookie_file()
            assert cookie_path == cookie_target and os.path.isfile(cookie_path)
            with open(cookie_path, encoding="utf-8") as fh:
                cookie_content = fh.read()
            assert "Netscape HTTP Cookie File" in cookie_content
            assert "sessionid\tSESSION_XYZ" in cookie_content
            # Без sessionid файл не создаётся.
            assert InstagramParser(apify_api_token="dummy")._create_cookie_file() == ""
        print("✅ Instagram _create_cookie_file (Netscape)")

        # --- _resolve_audio_path: разбор ответа yt-dlp -------------------- #
        assert InstagramParser._resolve_audio_path(
            {"requested_downloads": [{"filepath": "/tmp/reels_audio_AB12.webm"}]}
        ) == "/tmp/reels_audio_AB12.mp3"
        assert InstagramParser._resolve_audio_path({"id": "ZZ99"}) == "/tmp/reels_audio_ZZ99.mp3"
        assert InstagramParser._resolve_audio_path({}) == ""
        print("✅ Instagram _resolve_audio_path")

        # --- Диагностика ffmpeg: detect + автоотключение локального режима -- #
        with patch.object(this_mod.shutil, "which", return_value="/usr/local/bin/ffmpeg"):
            ig_with_ff = InstagramParser(apify_api_token="dummy")
        assert ig_with_ff._local_enabled is True
        assert ig_with_ff._ffmpeg_path == "/usr/local/bin/ffmpeg"

        with patch.object(this_mod.shutil, "which", return_value=None), patch.object(
            os.path, "isfile", return_value=False
        ):
            ig_no_ff = InstagramParser(apify_api_token="dummy")
        assert ig_no_ff._local_enabled is False
        assert ig_no_ff._ffmpeg_path == ""
        # parse() при отключённом локальном режиме идёт сразу в Apify, не трогая _download_audio.
        with patch.object(this_mod.shutil, "which", return_value=None), patch.object(
            os.path, "isfile", return_value=False
        ):
            ig_route = InstagramParser(apify_api_token="dummy")
        with patch.object(
            InstagramParser,
            "_fetch_ytdlp_info",
            new_callable=AsyncMock,
            return_value=_ig_metadata_empty,
        ), patch.object(
            InstagramParser, "_download_audio", new_callable=AsyncMock
        ) as mock_no_dl, patch.object(
            InstagramParser,
            "_fetch_apify_fallback",
            return_value=("FF Off T", "FF Off D", "FF Off Tr", ""),
        ):
            ff_off_out = await ig_route.parse("https://www.instagram.com/reel/noffmpeg123/")
        mock_no_dl.assert_not_awaited()
        assert "FF Off T" in ff_off_out.text and "FF Off Tr" in ff_off_out.text
        print("✅ Instagram _detect_ffmpeg + автоотключение локального режима")

        assert TikTokParser.can_parse("https://www.tiktok.com/@chef/video/1234567890")
        assert TikTokParser.can_parse("https://vm.tiktok.com/ABC123/")
        assert not TikTokParser.can_parse("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        print("✅ TikTok can_parse")

        _yt_metadata = {
            "title": "Mock Recipe Video Title Here",
            "description": "Line one\n\nIngredients: test flour 200g",
            "thumbnails": thumb_info["thumbnails"],
        }
        with patch.object(
            YouTubeParser,
            "_fetch_ytdlp_info",
            new_callable=AsyncMock,
            return_value=_yt_metadata,
        ), patch.object(
            YouTubeParser,
            "_download_audio",
            new_callable=AsyncMock,
            return_value="/tmp/youtube_audio_test.mp3",
        ), patch.object(
            YouTubeParser,
            "_transcribe_audio",
            new_callable=AsyncMock,
            return_value="Шаги из Whisper: смешать и запечь.",
        ), patch.object(
            YouTubeParser,
            "_fetch_subtitles_apify",
        ) as mock_yt_apify:
            yt_parsed = YouTubeParser(apify_api_token="dummy-apify")
            yt_parsed._local_enabled = True
            parsed = await yt_parsed.parse("https://www.youtube.com/watch?v=abcdefghijk")
        mock_yt_apify.assert_not_called()
        assert "Mock Recipe" in parsed.text
        assert "Ingredients" in parsed.text
        assert "приоритетный источник" in parsed.text
        assert "Whisper" in parsed.text or "запечь" in parsed.text
        print("✅ YouTube parse() yt-dlp + Whisper (мок)")

        with patch.object(
            YouTubeParser,
            "_fetch_ytdlp_info",
            new_callable=AsyncMock,
            return_value=_yt_metadata,
        ), patch.object(
            YouTubeParser,
            "_download_audio",
            new_callable=AsyncMock,
            side_effect=RuntimeError("no audio"),
        ), patch.object(
            YouTubeParser,
            "_fetch_subtitles_public",
            return_value="Публичные субтитры YouTube для теста",
        ) as mock_public, patch.object(
            YouTubeParser,
            "_fetch_subtitles_apify",
            return_value="SHOULD_NOT_BE_USED",
        ) as mock_apify_unused:
            yt_pub = YouTubeParser(apify_api_token="dummy")
            yt_pub._local_enabled = True
            parsed_pub = await yt_pub.parse("https://www.youtube.com/watch?v=abcdefghijk")
        mock_public.assert_called_once()
        mock_apify_unused.assert_not_called()
        assert "Ingredients" in parsed_pub.text
        assert "Публичные субтитры" in parsed_pub.text
        print("✅ YouTube: публичные captionTracks как supplement")

        _live_apify = os.getenv("APIFY_API_TOKEN", "").strip()
        if _live_apify:
            try:
                live_text = await YouTubeParser(apify_api_token=_live_apify).parse(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                )
                print(f"✅ YouTube live (yt-dlp): {len(live_text.text)} символов")
            except Exception as exc:  # noqa: BLE001
                print(f"⚠️  YouTube live: {exc}")

        registry = create_parser_registry()
        yt_p = registry.get_parser("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert yt_p is not None and getattr(yt_p, "source_type", None) == "youtube"
        ig_p = registry.get_parser("https://www.instagram.com/reel/abcd1234567/")
        assert ig_p is not None and getattr(ig_p, "source_type", None) == "instagram"
        tt_p = registry.get_parser("https://www.tiktok.com/@x/video/123")
        assert tt_p is not None and getattr(tt_p, "source_type", None) == "tiktok"
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
