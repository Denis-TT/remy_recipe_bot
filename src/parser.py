"""
Модуль парсеров рецептов Remy Bot.

Определяет расширяемую архитектуру для извлечения сырого текста
рецепта из разных источников:

* `BaseParser` — абстрактный базовый класс, описывающий контракт
  (методы `can_parse`, `parse`, свойство `source_type`).
* `WebParser` — конкретная реализация для обычных веб-страниц
  (HTTP(S)-сайты с рецептами).
* `ParserRegistry` — реестр парсеров, который автоматически
  выбирает подходящий парсер по URL.
* `create_parser_registry` — фабрика, возвращающая реестр с
  предустановленным набором парсеров.

В дальнейшем к иерархии добавятся `YouTubeShortsParser`,
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
from typing import Iterable, List, Optional

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

def create_parser_registry() -> ParserRegistry:
    """Построить реестр с набором парсеров по умолчанию.

    В текущем блоке проекта зарегистрирован только `WebParser`.
    По мере добавления специализированных парсеров (YouTube Shorts,
    Instagram Reels, TikTok) их экземпляры должны быть добавлены
    **раньше** `WebParser`, чтобы перехватывать соответствующие URL
    до общего fallback'а.

    Returns:
        Готовый к использованию `ParserRegistry`.
    """
    registry = ParserRegistry()
    # Сюда позже: registry.register(YouTubeShortsParser())
    # Сюда позже: registry.register(InstagramReelParser())
    # Сюда позже: registry.register(TikTokParser())
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
        registry = create_parser_registry()

        # Тест 1: can_parse через реестр
        assert registry.get_parser("https://eda.ru/test") is not None
        assert registry.get_parser("https://youtube.com/shorts/abc") is not None, (
            "WebParser должен пока ловить и YouTube-URL (нет специализированного парсера)"
        )
        assert registry.get_parser("not-a-url") is None
        assert registry.get_parser("ftp://example.com/file") is None
        print("✅ can_parse работает")

        # Тест 2: ValueError при пустом реестре
        empty = ParserRegistry()
        try:
            await empty.parse("https://eda.ru/")
        except ValueError as exc:
            print(f"✅ Пустой реестр корректно упал: {exc}")
        else:
            raise AssertionError("Ожидался ValueError от пустого реестра")

        # Тест 3: парсинг реальной страницы
        test_url = "https://eda.ru/recepty/supy/klassicheskij-borshh-34567"
        try:
            text = await registry.parse(test_url)
            print(f"✅ Извлечено {len(text)} символов")
            print(f"📝 Первые 200 символов: {text[:200]!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️  Тест с реальным URL пропущен: {exc}")

    asyncio.run(_test())
