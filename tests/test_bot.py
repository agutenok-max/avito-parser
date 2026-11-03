"""
Проверка команд и кнопок бота без реального Telegram: исходящие запросы
к Bot API перехватываются, входящие сообщения подаются вручную.
"""
import json

import pytest
import telebot
from telebot import apihelper
from telebot.types import Update

from database.monitor_db import MonitorDB
from telegram_bot.formatting import notification_text
from telegram_bot.handlers import register_handlers
from telegram_bot.monitor import Monitor
from tests.test_monitor import FakeFetcher, FakeNotifier

ADMIN = 111
STRANGER = 999


class Captured:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = kwargs.get("params") or {}
        self.calls.append((name, dict(params)))
        result = True
        if name in ("sendMessage", "editMessageText", "sendPhoto"):
            result = {"message_id": len(self.calls), "date": 0, "chat": {"id": ADMIN, "type": "private"},
                      "text": "x"}

        class R:
            status_code = 200
            text = json.dumps({"ok": True, "result": result})

            def json(self_inner):
                return json.loads(self_inner.text)
        return R()

    def texts(self):
        return [p.get("text", "") for n, p in self.calls if n in ("sendMessage", "editMessageText")]

    def last_text(self):
        return self.texts()[-1]


@pytest.fixture
def env(tmp_path):
    cap = Captured()
    apihelper.CUSTOM_REQUEST_SENDER = cap
    bot = telebot.TeleBot("123456:TEST", parse_mode="HTML", threaded=False)
    db = MonitorDB(str(tmp_path / "b.db"))
    mon = Monitor(db, FakeNotifier(), fetcher=FakeFetcher(), admin_ids=[ADMIN],
                  pause_between_searches=None, sleep=lambda s: None)
    register_handlers(bot, db, mon, admin_ids=[ADMIN])
    yield bot, db, mon, cap
    apihelper.CUSTOM_REQUEST_SENDER = None


_uid = [1]


def msg(bot, text, user=ADMIN):
    _uid[0] += 1
    upd = {"update_id": _uid[0], "message": {
        "message_id": _uid[0], "date": 0, "text": text,
        "chat": {"id": user, "type": "private"},
        "from": {"id": user, "is_bot": False, "first_name": "Тест"},
        "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else [],
    }}
    bot.process_new_updates([Update.de_json(upd)])


def press(bot, data, user=ADMIN):
    _uid[0] += 1
    upd = {"update_id": _uid[0], "callback_query": {
        "id": str(_uid[0]), "chat_instance": "1", "data": data,
        "from": {"id": user, "is_bot": False, "first_name": "Тест"},
        "message": {"message_id": 5, "date": 0, "text": "old", "chat": {"id": user, "type": "private"}},
    }}
    bot.process_new_updates([Update.de_json(upd)])


def test_start_and_access_control(env):
    bot, db, mon, cap = env
    msg(bot, "/start")
    assert "Здравствуйте" in cap.last_text() and str(ADMIN) in cap.last_text()
    msg(bot, "/start", user=STRANGER)
    assert "личный бот" in cap.last_text()
    press(bot, "pause", user=STRANGER)
    assert not mon.paused


def test_unconfigured_admin_shows_id(tmp_path):
    cap = Captured()
    apihelper.CUSTOM_REQUEST_SENDER = cap
    try:
        bot = telebot.TeleBot("123456:TEST", parse_mode="HTML", threaded=False)
        db = MonitorDB(str(tmp_path / "c.db"))
        mon = Monitor(db, FakeNotifier(), fetcher=FakeFetcher(), admin_ids=[])
        register_handlers(bot, db, mon, admin_ids=[])
        msg(bot, "/start", user=555)
        assert "TELEGRAM_ADMIN_ID" in cap.last_text() and "555" in cap.last_text()
        msg(bot, "/add https://www.avito.ru/moskva/velosipedy", user=555)
        assert db.list_searches() == []
    finally:
        apihelper.CUSTOM_REQUEST_SENDER = None


def test_add_list_remove_flow(env):
    bot, db, mon, cap = env
    press(bot, "add")
    assert "Пришлите ссылку" in cap.last_text()
    msg(bot, "https://example.com")
    assert "не ссылка Avito" in cap.last_text()
    msg(bot, "Смотри https://m.avito.ru/moskva/velosipedy?q=bmx")
    assert "добавлен" in cap.last_text()
    searches = db.list_searches(ADMIN)
    assert len(searches) == 1 and searches[0]["url"].endswith("s=104")
    assert searches[0]["id"] in mon._force  # первая проверка запланирована сразу

    msg(bot, "/add https://www.avito.ru/moskva/velosipedy?q=bmx")
    assert "уже отслеживается" in cap.last_text()

    msg(bot, "/list")
    assert "Мои поиски" in cap.last_text() and "bmx" in cap.last_text()
    sid = searches[0]["id"]
    press(bot, f"toggle:{sid}")
    assert db.get_search(sid)["is_active"] == 0
    msg(bot, "/remove")
    assert "Какой поиск удалить" in cap.last_text()
    press(bot, f"del:{sid}")
    assert "Удалить поиск" in cap.last_text()
    press(bot, f"delok:{sid}")
    assert db.list_searches(ADMIN) == []


def test_pause_resume_status_interval(env):
    bot, db, mon, cap = env
    msg(bot, "/pause")
    assert mon.paused
    msg(bot, "/status")
    assert "на паузе" in cap.last_text() and "Уведомлений отправлено" in cap.last_text()
    msg(bot, "/resume")
    assert not mon.paused
    msg(bot, "/interval 7")
    assert mon.interval_minutes == 7
    msg(bot, "/interval abc")
    assert "Укажите число" in cap.last_text()
    press(bot, "setint:15")
    assert mon.interval_minutes == 15
    msg(bot, "/interval")
    assert "15 мин" in cap.last_text()
    for button in ("menu", "status", "settings", "interval", "photos", "journal:0", "list", "remove",
                   "check_all", "pause", "resume"):
        press(bot, button)
    msg(bot, "/help")
    assert "Как пользоваться" in cap.last_text()


def test_notification_format_escapes_and_fits():
    item = {"title": "Велосипед <BMX> & Co", "price": "15 000 ₽", "address": "Москва",
            "url": "https://www.avito.ru/moskva/velosipedy/bmx_1", "description": "x" * 5000,
            "images": ["https://img"], "published_ts": None, "published_text": None}
    text = notification_text(item, "Поиск", with_photo=True)
    assert "&lt;BMX&gt; &amp; Co" in text
    assert "не указана на Avito" in text
    assert len(text) <= 1024
    item["published_text"] = "5 минут назад"
    text = notification_text(item, "Поиск", with_photo=False)
    assert "5 минут назад" in text and "Опубликовано только что" in text and "📷" in text
