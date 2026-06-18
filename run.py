#!/usr/bin/env python3
"""
Точка входа Remy Bot.

Отвечает за:
* защиту от множественного запуска (PID в ``/tmp/remy_bot.lock`` + ``os.kill(pid,0)``,
  удаление устаревшего lock, затем эксклюзивная блокировка ``fcntl``);
* настройку логирования (stdout + logs/remy.log с ротацией);
* опциональный HTTP healthcheck на порту 8081;
* корректную (graceful) остановку по SIGTERM/SIGINT.

Все пользовательские сообщения логируются на русском языке.
"""

from __future__ import annotations

import atexit
import errno
import fcntl
import json
import logging
import os
import shutil
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import FrameType
from typing import Optional

from config import config

# --------------------------------------------------------------------------- #
# Константы
# --------------------------------------------------------------------------- #

# Корень проекта — от него строим пути к logs/ и прочим ресурсам.
PROJECT_ROOT: Path = Path(__file__).resolve().parent

# Файл блокировки — лежит в /tmp, чтобы переживать только жизнь одного процесса.
LOCK_FILE_PATH: Path = Path("/tmp/remy_bot.lock")

# Путь к файлу логов и параметры ротации.
LOG_DIR: Path = PROJECT_ROOT / "logs"
LOG_FILE: Path = LOG_DIR / "remy.log"
LOG_MAX_BYTES: int = 5 * 1024 * 1024  # 5 МБ
LOG_BACKUP_COUNT: int = 3

# Healthcheck HTTP-сервер.
HEALTHCHECK_HOST: str = "0.0.0.0"
HEALTHCHECK_PORT: int = 8081

# Формат логов: "2026-04-24 12:00:00 | INFO | module | message".
LOG_FORMAT: str = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_DATEFMT: str = "%Y-%m-%d %H:%M:%S"


logger = logging.getLogger("remy")

# Глобальные ссылки на освобождаемые ресурсы.
_lock_file_handle: Optional[object] = None
_healthcheck_server: Optional[HTTPServer] = None
_bot_instance: Optional[object] = None
_shutdown_event = threading.Event()


# --------------------------------------------------------------------------- #
# Логирование
# --------------------------------------------------------------------------- #

def setup_logging() -> None:
    """Настроить корневой логгер: stdout + ротация файла logs/remy.log.

    Уровень берётся из `config.log_level`. Формат даты —
    `YYYY-MM-DD HH:MM:SS`, формат строки —
    `дата | уровень | имя | сообщение`.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, config.log_level, logging.INFO)
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATEFMT)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # На случай повторного вызова (например, в тестах) — убираем старые хендлеры.
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.setFormatter(formatter)
    stdout_handler.setLevel(level)
    root_logger.addHandler(stdout_handler)

    file_handler = RotatingFileHandler(
        filename=str(LOG_FILE),
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(level)
    root_logger.addHandler(file_handler)

    # Умеренный уровень шума от чужих библиотек.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Диагностика системных зависимостей
# --------------------------------------------------------------------------- #

def log_ffmpeg_diagnostics() -> None:
    """Залогировать наличие ffmpeg/ffprobe сразу при старте.

    ffmpeg нужен локальному распознаванию Instagram Reels (yt-dlp + Whisper).
    Этот лог показывает прямо в деплое Railway, доступны ли бинарники, ещё до
    обработки первого Reels. Функция чисто диагностическая и никогда не роняет
    процесс — на YouTube/Web-парсеры она не влияет.
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    if ffmpeg:
        logger.info("ffmpeg найден: %s", ffmpeg)
    else:
        logger.warning(
            "⚠️ ffmpeg не найден в PATH — локальное распознавание Reels отключится "
            "(fallback на Apify). Проверьте nixpacks.toml: [phases.setup] nixPkgs=[..., \"ffmpeg\"]."
        )

    if ffprobe:
        logger.info("ffprobe найден: %s", ffprobe)
    else:
        logger.warning("⚠️ ffprobe не найден в PATH (нужен yt-dlp для извлечения аудио).")


# --------------------------------------------------------------------------- #
# Защита от множественного запуска
# --------------------------------------------------------------------------- #


def _read_pid_from_lock_file() -> Optional[int]:
    """Прочитать PID из lock-файла или ``None``, если файла/числа нет."""
    try:
        raw = LOCK_FILE_PATH.read_text(encoding="utf-8").strip().split()
        if not raw:
            return None
        pid = int(raw[0])
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid: int) -> bool:
    """Проверить существование процесса без отправки сигнала (``kill(pid, 0)``)."""
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        # EPERM и др. — процесс может существовать, доступ ограничен.
        return True
    return True


def ensure_single_instance() -> None:
    """Гарантировать, что запущен ровно один экземпляр бота.

    1) Если ``/tmp/remy_bot.lock`` уже есть и PID из файла жив —
       немедленный выход с кодом 1 (защита от второго контейнера/процесса и 409 Conflict в Telegram).
    2) Если PID мёртв — удалить устаревший lock и продолжить.
    3) Эксклюзивная неблокирующая блокировка ``fcntl.lockf(LOCK_EX | LOCK_NB)`` на том же файле;
       при занятости — выход с кодом 1. FD держится до конца процесса, снятие через ``atexit``.
    """
    global _lock_file_handle

    if LOCK_FILE_PATH.is_file():
        stale_pid = _read_pid_from_lock_file()
        if stale_pid is not None:
            if _pid_is_alive(stale_pid):
                logger.error(
                    "❌ Уже запущен процесс бота (PID %s по файлу %s). "
                    "Повторный запуск приведёт к 409 Conflict в Telegram — завершение.",
                    stale_pid,
                    LOCK_FILE_PATH,
                )
                sys.exit(1)
            logger.warning(
                "⚠️ Устаревший lock (PID %s не существует), удаляю %s",
                stale_pid,
                LOCK_FILE_PATH,
            )
            try:
                LOCK_FILE_PATH.unlink()
            except OSError as exc:
                logger.error("❌ Не удалось удалить устаревший lock %s: %s", LOCK_FILE_PATH, exc)
                sys.exit(1)
        else:
            logger.warning(
                "⚠️ Lock-файл %s без корректного PID — удаляю",
                LOCK_FILE_PATH,
            )
            try:
                LOCK_FILE_PATH.unlink()
            except OSError as exc:
                logger.error("❌ Не удалось удалить повреждённый lock %s: %s", LOCK_FILE_PATH, exc)
                sys.exit(1)

    try:
        # Открываем в режиме записи, чтобы и создать файл, и удержать fd.
        lock_fp = open(LOCK_FILE_PATH, "w")
    except OSError as exc:
        logger.error("❌ Не удалось открыть файл блокировки %s: %s", LOCK_FILE_PATH, exc)
        sys.exit(1)

    try:
        fcntl.lockf(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("❌ Другой экземпляр бота уже запущен!")
        lock_fp.close()
        sys.exit(1)

    # Сохраняем PID внутрь файла блокировки — удобно при отладке.
    try:
        lock_fp.truncate(0)
        lock_fp.write(str(os.getpid()))
        lock_fp.flush()
    except OSError:
        # Не критично: блокировка уже захвачена, PID — это just-a-hint.
        pass

    _lock_file_handle = lock_fp
    atexit.register(_release_lock)


def _release_lock() -> None:
    """Освободить файловую блокировку при завершении процесса."""
    global _lock_file_handle

    if _lock_file_handle is None:
        return

    try:
        fcntl.lockf(_lock_file_handle, fcntl.LOCK_UN)
    except OSError:
        pass

    try:
        _lock_file_handle.close()
    except OSError:
        pass

    try:
        if LOCK_FILE_PATH.exists():
            LOCK_FILE_PATH.unlink()
    except OSError:
        pass

    _lock_file_handle = None
    logger.info("🧹 Блокировка снята, выход")


# --------------------------------------------------------------------------- #
# Healthcheck
# --------------------------------------------------------------------------- #

class _HealthcheckHandler(BaseHTTPRequestHandler):
    """Минимальный HTTP-обработчик healthcheck.

    Отвечает `{"status": "ok"}` на `GET /health`, иначе — 404.
    Встроенные логи BaseHTTPRequestHandler подавляем, чтобы не засорять stdout.
    """

    def do_GET(self) -> None:  # noqa: N802 — имя метода задано BaseHTTPRequestHandler
        if self.path == "/health":
            body = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — имя параметра зафиксировано родителем
        # Перенаправляем редкие сообщения в наш логгер уровнем DEBUG.
        logger.debug("healthcheck: " + format, *args)


def start_healthcheck_server() -> None:
    """Запустить HTTP healthcheck в отдельном daemon-потоке.

    Ошибки (например, занятый порт) не считаются фатальными: пишем
    предупреждение и продолжаем работу основного бота.
    """
    global _healthcheck_server

    try:
        server = HTTPServer((HEALTHCHECK_HOST, HEALTHCHECK_PORT), _HealthcheckHandler)
    except OSError as exc:
        logger.warning(
            "⚠️  Не удалось запустить healthcheck на порту %d: %s",
            HEALTHCHECK_PORT,
            exc,
        )
        return

    _healthcheck_server = server

    thread = threading.Thread(
        target=server.serve_forever,
        name="healthcheck",
        daemon=True,
    )
    thread.start()

    logger.info("🌐 Healthcheck запущен на порту %d", HEALTHCHECK_PORT)


def stop_healthcheck_server() -> None:
    """Остановить healthcheck, если он был запущен."""
    global _healthcheck_server

    if _healthcheck_server is None:
        return

    try:
        _healthcheck_server.shutdown()
        _healthcheck_server.server_close()
    except Exception as exc:  # noqa: BLE001 — нам всё равно, что именно сломалось
        logger.warning("⚠️  Ошибка при остановке healthcheck: %s", exc)

    _healthcheck_server = None


# --------------------------------------------------------------------------- #
# Graceful shutdown
# --------------------------------------------------------------------------- #

def _handle_shutdown_signal(signum: int, _frame: Optional[FrameType]) -> None:
    """Обработчик SIGTERM/SIGINT — инициирует корректную остановку."""
    try:
        signame = signal.Signals(signum).name
    except ValueError:
        signame = str(signum)

    logger.info("👋 Получен сигнал %s, останавливаем бота...", signame)
    _shutdown_event.set()

    # Пытаемся аккуратно закрыть бота, если он уже создан.
    if _bot_instance is not None:
        _stop_bot_safely(_bot_instance)

    stop_healthcheck_server()


def _register_signal_handlers() -> None:
    """Зарегистрировать обработчики сигналов завершения."""
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def _stop_bot_safely(bot: object) -> None:
    """Попытаться остановить бота, вызвав общеизвестные методы.

    Мы не знаем точный API будущего `RemyBot`, поэтому пробуем
    `stop()` и `shutdown()` в порядке убывания вероятности.
    """
    for method_name in ("stop", "shutdown", "close"):
        method = getattr(bot, method_name, None)
        if callable(method):
            try:
                method()
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning("⚠️  Ошибка при вызове %s() у бота: %s", method_name, exc)
                return


# --------------------------------------------------------------------------- #
# Запуск бота
# --------------------------------------------------------------------------- #

def _run_bot() -> None:
    """Импортировать и запустить `RemyBot`.

    Импорт выполняется лениво, чтобы логирование уже было настроено
    к моменту, когда бот начнёт что-то писать. Если модуль ещё не
    реализован (это первый блок проекта), пишем понятное сообщение
    и спокойно выходим, не падая со стек-трейсом.
    """
    global _bot_instance

    try:
        from src.bot import RemyBot  # noqa: WPS433 — отложенный импорт намеренный
    except ImportError:
        logger.warning(
            "⚠️  Модуль src.bot пока не реализован — это нормально для первого "
            "блока проекта. Фундамент (логирование, блокировка, healthcheck) "
            "работает. Ожидаю сигнал завершения..."
        )
        # Блокируемся до сигнала. Используем цикл с таймаутом: так
        # `Event.wait` возвращает управление периодически и гарантированно
        # замечает установку события обработчиком сигнала.
        while not _shutdown_event.is_set():
            _shutdown_event.wait(timeout=1.0)
        return

    logger.info("🤖 Создаём экземпляр RemyBot...")
    _bot_instance = RemyBot(config)

    start = getattr(_bot_instance, "start", None) or getattr(_bot_instance, "run", None)
    if not callable(start):
        logger.error("❌ У RemyBot нет методов start()/run(); нечего запускать")
        sys.exit(1)

    logger.info("🚀 Запускаем бота...")
    start()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> None:
    """Основная функция: защита, логирование, healthcheck, бот."""
    setup_logging()

    logger.info("🚀 Remy Bot запускается...")
    logger.info("✅ Конфигурация загружена (окружение: %s)", config.environment)
    logger.info("📝 Логирование настроено (уровень: %s)", config.log_level)

    log_ffmpeg_diagnostics()

    logger.info("🔒 Проверка единственного экземпляра...")
    ensure_single_instance()
    logger.info("✅ Запущен единственный экземпляр бота")

    _register_signal_handlers()

    start_healthcheck_server()

    try:
        _run_bot()
    except KeyboardInterrupt:
        logger.info("👋 Получен KeyboardInterrupt, останавливаем бота...")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("❌ Ошибка запуска: %s", exc)
        sys.exit(1)
    finally:
        stop_healthcheck_server()
        # _release_lock зарегистрирован через atexit и вызовется автоматически.


if __name__ == "__main__":
    main()
