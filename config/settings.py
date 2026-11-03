"""
Единые настройки проекта.

Все значения берутся из переменных окружения (или файла .env при локальном
запуске). Секреты (TELEGRAM_BOT_TOKEN и т.п.) НИКОГДА не хранятся в коде.
"""
import os
import re
import sys
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv
from loguru import logger

# Загрузка .env (если файла нет — ничего страшного, используются переменные окружения)
load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().split("#")[0].strip().lower() in ("1", "true", "yes", "on", "да")


def _env_int(name: str, default: int) -> int:
    value = (os.getenv(name) or "").split("#")[0].strip()
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    # Убираем inline-комментарии вида "value  # comment"
    value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    return value if value else default


def _env_ids(name: str) -> List[int]:
    raw = _env_str(name, "")
    ids = []
    for part in re.split(r"[,\s;]+", raw):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


@dataclass
class Settings:
    # === Старые настройки CLI-парсера (main.py) — сохранены для совместимости ===
    target_url: str = field(default_factory=lambda: _env_str("TARGET_URL", ""))
    parser_mode: str = field(default_factory=lambda: _env_str("PARSER_MODE", "playwright"))
    log_level: str = field(default_factory=lambda: _env_str("LOG_LEVEL", "INFO").upper())
    use_antibot_tricks: bool = field(default_factory=lambda: _env_bool("USE_ANTIBOT_TRICKS", False))
    use_local_html: bool = field(default_factory=lambda: _env_bool("USE_LOCAL_HTML", False))
    local_html_path: str = field(default_factory=lambda: _env_str("LOCAL_HTML_PATH", ""))

    # === Telegram-бот ===
    telegram_bot_token: str = field(
        default_factory=lambda: _env_str("TELEGRAM_BOT_TOKEN", "") or _env_str("telegram_bot_token", "")
    )
    admin_ids: List[int] = field(default_factory=lambda: _env_ids("TELEGRAM_ADMIN_ID"))

    # === Хранилище ===
    data_dir: str = field(default_factory=lambda: _env_str("DATA_DIR", "data"))

    # === Мониторинг ===
    check_interval_minutes: int = field(default_factory=lambda: _env_int("CHECK_INTERVAL_MINUTES", 3))
    min_interval_minutes: int = field(default_factory=lambda: _env_int("MIN_INTERVAL_MINUTES", 2))
    max_searches: int = field(default_factory=lambda: _env_int("MAX_SEARCHES", 20))
    max_notifications_per_check: int = field(
        default_factory=lambda: _env_int("MAX_NOTIFICATIONS_PER_CHECK", 10)
    )
    send_photos: bool = field(default_factory=lambda: _env_bool("SEND_PHOTOS", True))
    # auto — сначала лёгкий HTTP-запрос, при неудаче браузер; http — только HTTP; browser — только браузер
    fetch_mode: str = field(default_factory=lambda: _env_str("FETCH_MODE", "auto").lower())
    request_timeout: int = field(default_factory=lambda: _env_int("REQUEST_TIMEOUT", 30))
    # Пауза после блокировки со стороны Avito (минуты): первая и максимальная
    block_pause_minutes: int = field(default_factory=lambda: _env_int("BLOCK_PAUSE_MINUTES", 15))
    block_pause_max_minutes: int = field(default_factory=lambda: _env_int("BLOCK_PAUSE_MAX_MINUTES", 120))
    # Необязательный HTTP(S)-прокси, например http://user:pass@host:port
    proxy_url: str = field(default_factory=lambda: _env_str("PROXY_URL", ""))
    # Стоп-слова в заголовке (через запятую). По умолчанию пусто — фильтр выключен.
    stop_words: List[str] = field(
        default_factory=lambda: [w.strip().lower() for w in _env_str("STOP_WORDS", "").split(",") if w.strip()]
    )
    timezone: str = field(default_factory=lambda: _env_str("TZ", "Europe/Moscow"))

    # === Проверка состояния (health check) ===
    health_port: int = field(default_factory=lambda: _env_int("PORT", _env_int("HEALTH_PORT", 8080)))
    health_enabled: bool = field(default_factory=lambda: _env_bool("HEALTH_ENABLED", True))

    @property
    def db_path(self) -> str:
        return _env_str("DB_PATH", os.path.join(self.data_dir, "avito_bot.db"))

    @property
    def logs_dir(self) -> str:
        return _env_str("LOGS_DIR", os.path.join(self.data_dir, "logs"))


settings = Settings()


# === Защита секретов в логах ===
def _secrets() -> List[str]:
    values = [settings.telegram_bot_token, settings.proxy_url]
    return [v for v in values if v and len(v) >= 6]


def redact(text: str) -> str:
    """Удаляет секреты (токен бота, прокси) из строки."""
    if not text:
        return text
    for secret in _secrets():
        text = text.replace(secret, "***")
    # Любой токен Telegram вида 123456:ABC... тоже маскируем
    text = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b", "***", text)
    return text


def _patch_record(record):
    record["message"] = redact(record["message"])
    if record["exception"] is not None:
        # Текст исключения тоже может содержать URL с токеном
        exc = record["exception"]
        if exc.value is not None:
            try:
                exc.value.args = tuple(redact(str(a)) for a in exc.value.args)
            except Exception:
                pass


def setup_logging() -> None:
    logger.remove()
    logger.configure(patcher=_patch_record)
    logger.add(
        sys.stdout,
        level=settings.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )
    try:
        os.makedirs(settings.logs_dir, exist_ok=True)
        logger.add(
            os.path.join(settings.logs_dir, "app.log"),
            level="DEBUG",
            rotation="10 MB",
            retention=5,
            encoding="utf-8",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        )
    except Exception as e:  # pragma: no cover - например, read-only FS
        logger.warning(f"Не удалось создать файл логов: {e}")

    # Перенаправляем стандартный logging (используется pyTelegramBotAPI) в loguru,
    # чтобы на него тоже действовала маскировка секретов.
    import logging

    class _InterceptHandler(logging.Handler):
        def emit(self, rec):
            try:
                level = logger.level(rec.levelname).name
            except Exception:
                level = rec.levelno
            logger.opt(depth=6, exception=rec.exc_info).log(level, rec.getMessage())

    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.WARNING, force=True)


setup_logging()
