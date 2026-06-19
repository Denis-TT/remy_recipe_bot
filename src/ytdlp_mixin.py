"""
Общий пайплайн yt-dlp + faster-whisper для коротких видео (Reels, YouTube, TikTok, VK).

  1. Метаданные и обложка через yt-dlp (без скачивания видео);
  2. Аудио + faster-whisper — шаги из видео;
  3. Описание и речь объединяются (описание — приоритет);
  4. При сбое — Apify-fallback (реализуется в наследниках).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
import uuid
from abc import abstractmethod
from typing import Any, Awaitable, Callable, Optional

import aiohttp
from config import config as _remy_config

logger = logging.getLogger(__name__)

IMAGES_DIR: str = str(_remy_config.images_dir or "/images").rstrip("/") or "/images"

ProgressCallback = Callable[[str, str], Awaitable[None]]


def _images_dir() -> str:
    """Актуальный каталог изображений (тесты патчат ``parser.IMAGES_DIR``)."""
    from . import parser as parser_mod

    return parser_mod.IMAGES_DIR


def ensure_images_dir() -> None:
    """Гарантировать существование каталога (делегирует в ``parser``)."""
    from .parser import ensure_images_dir as _ensure

    _ensure()


def safe_video_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def pick_best_thumbnail(info: Optional[dict]) -> str:
    """Выбрать URL обложки максимального качества из метаданных yt-dlp."""
    info = info or {}
    thumbs = info.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        best: Optional[dict] = None
        best_score = -1
        for item in thumbs:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            score = int(item.get("height") or 0) * int(item.get("width") or 0)
            if score <= 0:
                score = len(url)
            if score > best_score:
                best_score = score
                best = item
        if best is not None:
            return str(best.get("url") or "").strip()
    return str(info.get("thumbnail") or "").strip()


def video_compose_text(
    platform: str,
    title: str,
    description: str,
    transcript: str,
) -> str:
    """Собрать сырой текст: описание в приоритете, речь/субтитры — дополнение."""
    chunks: list[str] = []
    if title.strip():
        chunks.append(f"Название ({platform}):\n{title.strip()}")
    if description.strip():
        chunks.append(
            f"Описание ({platform}, приоритетный источник):\n{description.strip()}"
        )
    if transcript.strip():
        chunks.append(
            f"Речь в видео / субтитры ({platform}, дополнение):\n{transcript.strip()}"
        )
    return "\n\n".join(chunks)


def metadata_from_ytdlp_info(
    info: Optional[dict],
    *,
    prepend_uploader: bool = True,
    title_cleaner: Optional[Callable[[str], str]] = None,
    description_cleaner: Optional[Callable[[str], str]] = None,
) -> tuple[str, str, str]:
    """Извлечь название, описание и URL обложки из ответа yt-dlp."""
    info = info or {}
    title = safe_video_str(info.get("title") or info.get("fulltitle"))
    description = safe_video_str(info.get("description"))
    if title_cleaner:
        title = title_cleaner(title)
    if description_cleaner:
        description = description_cleaner(description)
    if not description:
        description = title
        title = ""
    elif title and description == title:
        title = ""
    if prepend_uploader:
        uploader = safe_video_str(info.get("uploader") or info.get("channel"))
        if uploader and uploader not in description:
            handle = uploader if uploader.startswith("@") else f"@{uploader}"
            description = f"{handle}\n\n{description}".strip()
    thumb_url = pick_best_thumbnail(info)
    return title, description, thumb_url


class YtdlpWhisperMixin:
    """Миксин: yt-dlp метаданные + Whisper + объединение с описанием."""

    MAX_TEXT_LENGTH: int = 50_000
    TIMEOUT_SECONDS: float = 30.0
    WHISPER_MODEL_NAME: str = "small"
    WHISPER_COMPUTE_TYPE: str = "int8"
    TRANSCRIBE_TIMEOUT_SECONDS: float = 120.0
    FFMPEG_FALLBACK_PATH: str = "/usr/bin/ffmpeg"
    FFPROBE_FALLBACK_PATH: str = "/usr/bin/ffprobe"
    HEADERS: dict

    # Переопределяется в наследниках.
    PLATFORM_NAME: str = "видео"
    AUDIO_OUTTMPL: str = "/tmp/video_audio_%(id)s.%(ext)s"
    AUDIO_LOG_LABEL: str = "видео"

    _whisper_model: Any
    _ffmpeg_path: str
    _ffprobe_path: str
    _local_enabled: bool

    @property
    @abstractmethod
    def source_type(self) -> str:
        ...

    @classmethod
    def _detect_ffmpeg(cls) -> tuple[str, str]:
        ffmpeg = shutil.which("ffmpeg") or ""
        if not ffmpeg and os.path.isfile(cls.FFMPEG_FALLBACK_PATH):
            ffmpeg = cls.FFMPEG_FALLBACK_PATH
        ffprobe = shutil.which("ffprobe") or ""
        if not ffprobe and os.path.isfile(cls.FFPROBE_FALLBACK_PATH):
            ffprobe = cls.FFPROBE_FALLBACK_PATH
        return ffmpeg, ffprobe

    def _init_ytdlp_whisper(self) -> None:
        self._whisper_model = None
        self._ffmpeg_path, self._ffprobe_path = self._detect_ffmpeg()
        self._local_enabled = bool(self._ffmpeg_path)
        if self._local_enabled:
            logger.info("ffmpeg найден: %s", self._ffmpeg_path)
        else:
            logger.warning(
                "%s: ffmpeg не найден, локальное распознавание отключено",
                type(self).__name__,
            )

    def _metadata_from_info(self, info: Optional[dict]) -> tuple[str, str, str]:
        return metadata_from_ytdlp_info(info, prepend_uploader=True)

    def _ytdlp_cookiefile(self) -> str:
        """Путь к cookie-файлу для yt-dlp или пустая строка."""
        return ""

    def _extra_ytdlp_opts(self) -> dict[str, Any]:
        return {}

    async def parse_ytdlp_video(
        self,
        url: str,
        *,
        on_progress: Optional[ProgressCallback] = None,
        supplement_transcript: Optional[Callable[[str], str]] = None,
    ) -> Any:
        """Универсальный пайплайн: метаданные → Whisper → объединение → Apify."""
        from .parser import ParseResult

        async def _notify(stage: str, detail: str = "") -> None:
            if on_progress is not None:
                await on_progress(stage, detail)

        title = ""
        description = ""
        image_path: Optional[str] = None

        await _notify("fetching_metadata")
        try:
            info = await self._fetch_ytdlp_info(url, download=False)
            title, description, thumb_url = self._metadata_from_info(info)
            if title or description:
                logger.info(
                    "%s: метаданные title=%d симв., description=%d симв.",
                    type(self).__name__,
                    len(title),
                    len(description),
                )
            if thumb_url:
                logger.info("%s: обложка найдена через yt-dlp", type(self).__name__)
                image_path = await self._download_thumbnail(thumb_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s: не удалось получить метаданные yt-dlp: %s",
                type(self).__name__,
                exc,
            )

        transcript = ""
        if self._local_enabled:
            await _notify("downloading_audio")
            try:
                audio_path = await self._download_audio(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "⚠️ %s: локальное распознавание не удалось: %s",
                    type(self).__name__,
                    exc,
                )
                audio_path = ""

            if audio_path:
                await _notify("transcribing")
                try:
                    transcript = await asyncio.wait_for(
                        self._transcribe_audio(audio_path, on_progress=on_progress),
                        timeout=self.TRANSCRIBE_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "⚠️ %s: Whisper превысил %.0f с",
                        type(self).__name__,
                        self.TRANSCRIBE_TIMEOUT_SECONDS,
                    )
                    transcript = ""
                if transcript.strip():
                    logger.info("✅ %s: локальное распознавание речи успешно", type(self).__name__)
        else:
            logger.info(
                "ℹ️ %s: локальный режим отключён (нет ffmpeg)",
                type(self).__name__,
            )
            await _notify("apify_fallback", "нет ffmpeg")

        if supplement_transcript and not transcript.strip():
            extra = supplement_transcript(url)
            if extra.strip():
                transcript = extra.strip()
                logger.info(
                    "%s: дополнительный транскрипт (%d симв.)",
                    type(self).__name__,
                    len(transcript),
                )

        text = video_compose_text(self.PLATFORM_NAME, title, description, transcript)
        if text.strip():
            if len(text) > self.MAX_TEXT_LENGTH:
                text = text[: self.MAX_TEXT_LENGTH]
            return ParseResult(text=text, image_url=image_path)

        logger.warning(
            "⚠️ %s: локально недостаточно данных — Apify",
            type(self).__name__,
        )
        await _notify("apify_fallback")
        apify_title, apify_desc, apify_tr, apify_img = await asyncio.to_thread(
            self._fetch_apify_fallback,
            url,
        )
        if apify_title and not title.strip():
            title = apify_title
        if apify_desc and not description.strip():
            description = apify_desc
        if apify_tr and not transcript.strip():
            transcript = apify_tr
        if not image_path and apify_img:
            image_path = await self._download_thumbnail(apify_img)

        text = video_compose_text(self.PLATFORM_NAME, title, description, transcript)
        if not text.strip():
            raise RuntimeError(f"Не удалось извлечь текст из {self.PLATFORM_NAME}")
        if len(text) > self.MAX_TEXT_LENGTH:
            text = text[: self.MAX_TEXT_LENGTH]
        return ParseResult(text=text, image_url=image_path)

    @abstractmethod
    def _fetch_apify_fallback(self, url: str) -> tuple[str, str, str, str]:
        """title, description, transcript, image_url."""

    def _ytdlp_base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            **self._extra_ytdlp_opts(),
        }
        if self._ffmpeg_path:
            opts["ffmpeg_location"] = os.path.dirname(self._ffmpeg_path) or self._ffmpeg_path
        return opts

    async def _fetch_ytdlp_info(self, url: str, *, download: bool = False) -> dict:
        import yt_dlp  # type: ignore[import-untyped]

        ydl_opts = self._ytdlp_base_opts()
        if not download:
            ydl_opts["skip_download"] = True

        cookie_path = self._ytdlp_cookiefile()
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path

        def _extract() -> dict:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                result = ydl.extract_info(url, download=download)
                if not isinstance(result, dict):
                    raise RuntimeError("yt-dlp вернул пустые метаданные")
                return result

        try:
            return await asyncio.to_thread(_extract)
        finally:
            if cookie_path and os.path.isfile(cookie_path):
                with contextlib.suppress(OSError):
                    os.remove(cookie_path)

    async def _download_audio(self, url: str) -> str:
        if not self._ffmpeg_path:
            raise RuntimeError("Локальный режим недоступен: ffmpeg не найден")

        import yt_dlp  # type: ignore[import-untyped]

        logger.info("🎵 Скачиваю аудио (%s)...", self.AUDIO_LOG_LABEL)

        ydl_opts: dict[str, Any] = {
            **self._ytdlp_base_opts(),
            "format": "bestaudio/best",
            "outtmpl": self.AUDIO_OUTTMPL,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }

        cookie_path = self._ytdlp_cookiefile()
        if cookie_path:
            ydl_opts["cookiefile"] = cookie_path

        def _extract() -> dict:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=True)

        try:
            info = await asyncio.to_thread(_extract)
        except Exception as exc:  # noqa: BLE001
            logger.error("❌ Ошибка скачивания аудио (%s): %s", self.AUDIO_LOG_LABEL, exc)
            raise
        finally:
            if cookie_path and os.path.isfile(cookie_path):
                with contextlib.suppress(OSError):
                    os.remove(cookie_path)

        audio_path = self._resolve_audio_path(info)
        if not audio_path or not os.path.isfile(audio_path):
            raise RuntimeError(
                f"Аудиофайл не найден после скачивания (id={(info or {}).get('id')!r})"
            )
        logger.info("✅ Аудио скачано: %s", audio_path)
        return audio_path

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
            return f"/tmp/video_audio_{video_id}.mp3"
        return ""

    async def _transcribe_audio(
        self,
        audio_path: str,
        *,
        on_progress: Optional[ProgressCallback] = None,
    ) -> str:
        heartbeat_task: Optional[asyncio.Task[None]] = None
        if on_progress is not None:

            async def _heartbeat() -> None:
                start = time.monotonic()
                while True:
                    await asyncio.sleep(12)
                    elapsed = int(time.monotonic() - start)
                    await on_progress("transcribing", f"{elapsed} с")

            heartbeat_task = asyncio.create_task(_heartbeat())

        try:
            model = await self._ensure_whisper_model()
            logger.info(
                "🎙️ Распознаю речь через faster-whisper (%s)...",
                self.AUDIO_LOG_LABEL,
            )
            text = await asyncio.to_thread(self._transcribe_sync, model, audio_path)
            logger.info("✅ Распознано %d символов", len(text))
            return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("⚠️ Ошибка распознавания Whisper: %s", exc)
            return ""
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if audio_path and os.path.isfile(audio_path):
                with contextlib.suppress(OSError):
                    os.remove(audio_path)

    @staticmethod
    def _transcribe_sync(model: Any, audio_path: str) -> str:
        segments, _info = model.transcribe(
            audio_path,
            language="ru",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        parts = [seg.text.strip() for seg in segments if seg.text and seg.text.strip()]
        return " ".join(parts)

    async def _ensure_whisper_model(self) -> Any:
        if self._whisper_model is None:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]

            logger.info(
                "⏳ Загружаю faster-whisper '%s' (%s)...",
                self.WHISPER_MODEL_NAME,
                self.WHISPER_COMPUTE_TYPE,
            )
            self._whisper_model = await asyncio.to_thread(
                WhisperModel,
                self.WHISPER_MODEL_NAME,
                device="cpu",
                compute_type=self.WHISPER_COMPUTE_TYPE,
            )
        return self._whisper_model

    async def preload_whisper_model(self) -> None:
        if not self._local_enabled:
            return
        await self._ensure_whisper_model()
        logger.info("✅ Модель faster-whisper '%s' предзагружена", self.WHISPER_MODEL_NAME)

    async def _download_thumbnail(self, img_url: str) -> Optional[str]:
        url = (img_url or "").strip()
        if not url or not url.startswith(("http://", "https://")):
            return None

        ensure_images_dir()
        dest = os.path.join(_images_dir(), f"{uuid.uuid4().hex}.jpg")
        try:
            ok = await self._download_binary_to_path(url, dest)
            if ok:
                logger.info("Обложка сохранена: %s", dest)
                return dest
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("Не удалось скачать обложку: %s", exc)
        if os.path.isfile(dest):
            with contextlib.suppress(OSError):
                os.unlink(dest)
        return None

    async def _download_binary_to_path(self, file_url: str, dest_path: str) -> bool:
        timeout = aiohttp.ClientTimeout(total=self.TIMEOUT_SECONDS)
        max_bytes = 8 * 1024 * 1024
        async with aiohttp.ClientSession(timeout=timeout, headers=self.HEADERS) as session:
            async with session.get(file_url, allow_redirects=True) as response:
                if response.status >= 400:
                    return False
                data = await response.read()
        if len(data) > max_bytes:
            return False
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
