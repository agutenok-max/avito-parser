#!/usr/bin/env python3
"""
Точка входа: Telegram-бот + фоновый мониторинг Avito + health check.

Запуск:  python -m telegram_bot.bot   (или python telegram_bot/bot.py)
"""
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402
import telebot  # noqa: E402
from telebot import apihelper  # noqa: E402
from telebot.apihelper import ApiTelegramException  # noqa: E402

from config.settings import logger, settings  # noqa: E402
from database.monitor_db import MonitorDB  # noqa: E402
from telegram_bot import keyboards as kb  # noqa: E402
from telegram_bot.formatting import notification_text  # noqa: E402
from telegram_bot.handlers import register_handlers, setup_commands  # noqa: E402
from telegram_bot.monitor import Monitor, Notifier  # noqa: E402

STARTED_AT = time.time()


class TelegramNotifier(Notifier):
    def __init__(self, bot: telebot.TeleBot, db: MonitorDB):
        self.bot = bot
        self.db = db

    def _call(self, func, *args, **kwargs):
        """Вызов Telegram API с повтором при лимите (429) и сетевых сбоях."""
        for attempt in range(3):
            try:
                return func(*args, **kwargs)
            except ApiTelegramException as e:
                if e.error_code == 429:
                    retry = 5
                    try:
                        retry = int(e.result_json.get("parameters", {}).get("retry_after", 5))
                    except Exception:
                        pass
                    time.sleep(min(retry, 60) + 1)
                    continue
                raise
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))
        raise RuntimeError("Telegram API недоступен")

    def send_listing(self, chat_id: int, item: Dict, search: Dict) -> Optional[int]:
        markup = kb.listing_buttons(item["url"])
        photos = bool(self.db.get("send_photos", settings.send_photos))
        image = (item.get("images") or [None])[0] if photos else None
        if image:
            caption = notification_text(item, search["title"], with_photo=True)
            # 1) Telegram сам скачивает фото по ссылке
            try:
                msg = self._call(self.bot.send_photo, chat_id, image, caption=caption, reply_markup=markup)
                return msg.message_id
            except ApiTelegramException as e:
                logger.debug(f"Фото по ссылке не отправилось ({e.description}), загружаю файлом")
            # 2) Скачиваем фото сами и загружаем файлом
            data = self._download_image(image)
            if data:
                try:
                    msg = self._call(self.bot.send_photo, chat_id, data, caption=caption, reply_markup=markup)
                    return msg.message_id
                except ApiTelegramException as e:
                    logger.debug(f"Фото файлом не отправилось ({e.description}), отправляю текстом")
        # 3) Без фото — обычным сообщением
        msg = self._call(self.bot.send_message, chat_id,
                         notification_text(item, search["title"], with_photo=False),
                         reply_markup=markup, disable_web_page_preview=True)
        return msg.message_id

    @staticmethod
    def _download_image(url: str) -> Optional[bytes]:
        try:
            resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"},
                                proxies={"http": settings.proxy_url, "https": settings.proxy_url}
                                if settings.proxy_url else None)
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and ctype.startswith("image/") and 0 < len(resp.content) < 10_000_000:
                return resp.content
        except requests.RequestException:
            pass
        return None

    def send_text(self, chat_id: int, text: str, url_button: Optional[tuple] = None) -> None:
        markup = None
        if url_button:
            markup = telebot.types.InlineKeyboardMarkup().add(
                telebot.types.InlineKeyboardButton(url_button[0], url=url_button[1]))
        self._call(self.bot.send_message, chat_id, text, reply_markup=markup, disable_web_page_preview=True)


# ------------------------------------------------------------------ health
class _State:
    monitor: Optional[Monitor] = None


def health_payload() -> (int, dict):
    m = _State.monitor
    now = time.time()
    monitor_ok = bool(m and m.is_alive() and now - m.heartbeat < 300)
    body = {
        "status": "ok" if monitor_ok else "degraded",
        "uptime_seconds": int(now - STARTED_AT),
        "monitor_alive": monitor_ok,
    }
    if m:
        st = m.status()
        body.update({
            "paused": st["paused"],
            "active_searches": st["searches_active"],
            "last_check_at": st["last_check_at"],
            "next_check_at": st["next_check_at"],
            "blocked_until": st["blocked_until"],
        })
    return (200 if monitor_ok else 503), body


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        code, body = health_payload() if self.path.startswith("/health") else (200, {"service": "avito-bot"})
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_health_server() -> None:
    if not settings.health_enabled:
        return
    # Слушаем порт из настроек (PORT, по умолчанию 8080) и, на всякий случай, 8080 —
    # он указан в amvera.yml (containerPort).
    for port in sorted({settings.health_port, 8080}):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
        except OSError as e:
            logger.warning(f"Health-check сервер не запущен на порту {port}: {e}")
            continue
        threading.Thread(target=server.serve_forever, name=f"health-{port}", daemon=True).start()
        logger.info(f"Health check: http://0.0.0.0:{port}/health")


def start_watchdog(monitor: Monitor, stale_minutes: int = 20) -> None:
    """Если поток мониторинга завис или упал — завершаем процесс, хостинг перезапустит контейнер."""
    def _watch():
        while True:
            time.sleep(60)
            stale = time.time() - monitor.heartbeat
            if not monitor.is_alive() or stale > stale_minutes * 60:
                logger.error(f"Мониторинг не отвечает ({int(stale)} с). Перезапуск процесса…")
                os._exit(1)
    threading.Thread(target=_watch, name="watchdog", daemon=True).start()


def check_storage() -> Dict:
    """Проверяет, что база лежит в постоянном хранилище и туда можно писать."""
    data_dir = os.path.abspath(settings.data_dir)
    info = {"path": os.path.abspath(settings.db_path), "persistent": None, "writable": False}
    try:
        os.makedirs(data_dir, exist_ok=True)
        probe = os.path.join(data_dir, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        info["writable"] = True
    except OSError as e:
        logger.error(f"❌ Нет доступа на запись в {data_dir}: {e}")
    if os.getenv("AMVERA") == "1" or data_dir == "/data":
        info["persistent"] = os.path.ismount(data_dir)
        if not info["persistent"]:
            logger.warning(f"⚠️ {data_dir} не является подключённым постоянным хранилищем — "
                           "база может потеряться при пересборке. Проверьте persistenceMount в amvera.yml.")
        else:
            logger.info(f"Постоянное хранилище подключено: {data_dir}")
    return info


# -------------------------------------------------------------------- main
def main():
    token = settings.telegram_bot_token
    if not token:
        logger.error("❌ Не задан TELEGRAM_BOT_TOKEN. Добавьте его в переменные окружения хостинга "
                     "(или в файл .env при локальном запуске). Инструкция: DEPLOY.md")
        sys.exit(1)

    if not settings.admin_ids:
        logger.warning("⚠️ TELEGRAM_ADMIN_ID не задан: бот ответит на /start вашим ID, "
                       "но управлять им можно будет только после добавления этой переменной.")

    apihelper.CONNECT_TIMEOUT = 30
    apihelper.READ_TIMEOUT = 90
    if settings.proxy_url and os.getenv("TELEGRAM_USE_PROXY", "false").lower() == "true":
        apihelper.proxy = {"https": settings.proxy_url}

    storage = check_storage()
    db = MonitorDB(settings.db_path)
    db.set("storage_persistent", storage["persistent"])
    logger.info(f"База данных: {storage['path']}")

    bot = telebot.TeleBot(token, parse_mode='HTML', threaded=True, num_threads=4)
    notifier = TelegramNotifier(bot, db)
    monitor = Monitor(db, notifier)
    _State.monitor = monitor

    register_handlers(bot, db, monitor)

    # Проверяем токен заранее, чтобы показать понятную ошибку
    for attempt in range(1, 6):
        try:
            me = bot.get_me()
            logger.success(f"✅ Бот @{me.username} подключён к Telegram")
            break
        except ApiTelegramException as e:
            if e.error_code == 401:
                logger.error("❌ Telegram отклонил токен (401). Проверьте TELEGRAM_BOT_TOKEN.")
                sys.exit(1)
            logger.warning(f"Telegram недоступен (попытка {attempt}/5): {e}")
        except Exception as e:
            logger.warning(f"Telegram недоступен (попытка {attempt}/5): {type(e).__name__}")
        time.sleep(min(60, 5 * attempt))

    # Если раньше у бота был настроен webhook, getUpdates не работает — снимаем его
    try:
        bot.remove_webhook()
    except Exception as e:
        logger.warning(f"Не удалось снять webhook: {type(e).__name__}")

    setup_commands(bot)
    start_health_server()
    monitor.start()
    start_watchdog(monitor)

    def _shutdown(signum, frame):
        logger.info("Получен сигнал остановки, завершаю работу…")
        monitor.stop()
        bot.stop_polling()

    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except ValueError:  # запуск не из главного потока (тесты)
        pass

    logger.success(f"✅ Avito Monitor Bot запущен. Интервал: {monitor.interval_minutes} мин., "
                   f"режим загрузки: {settings.fetch_mode}")
    try:
        # infinity_polling сам переподключается при сетевых ошибках
        bot.infinity_polling(timeout=30, long_polling_timeout=30, skip_pending=True,
                             allowed_updates=["message", "callback_query"])
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
