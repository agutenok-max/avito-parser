"""
Сквозной тест: запускается настоящий bot.main() (Telegram-сервер имитируется,
страницы Avito подставляются). Проверяется весь путь пользователя.
"""
import json
import threading
import time

import pytest
import telebot
from telebot import apihelper

from services import avito_fetcher
from tests.fixtures import mfe_item, mfe_page

ADMIN = 111
NOW_MS = int(time.time() * 1000)


class FakeTelegram:
    def __init__(self):
        self.lock = threading.Lock()
        self.inbox, self.out, self.uid = [], [], 100

    def user_sends(self, text=None, data=None, user=ADMIN):
        with self.lock:
            self.uid += 1
            frm = {"id": user, "is_bot": False, "first_name": "Иван"}
            chat = {"id": user, "type": "private"}
            if data:
                upd = {"update_id": self.uid, "callback_query": {
                    "id": str(self.uid), "chat_instance": "1", "data": data, "from": frm,
                    "message": {"message_id": 1, "date": 0, "text": "m", "chat": chat}}}
            else:
                ent = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
                upd = {"update_id": self.uid, "message": {"message_id": self.uid, "date": 0, "text": text,
                                                          "chat": chat, "from": frm, "entities": ent}}
            self.inbox.append(upd)

    def __call__(self, method, url, **kw):
        name = url.rsplit("/", 1)[-1]
        p = dict(kw.get("params") or {})
        res = True
        if name == "getUpdates":
            time.sleep(0.05)
            with self.lock:
                off = int(p.get("offset") or 0)
                res = [u for u in self.inbox if u["update_id"] >= off] if off > 0 else []
                if off == -1 or off == 0:
                    res = []
        elif name == "getMe":
            res = {"id": 1, "is_bot": True, "first_name": "B", "username": "e2e_bot"}
        elif name in ("sendMessage", "sendPhoto", "editMessageText"):
            with self.lock:
                self.out.append((name, p.get("chat_id"), p.get("text") or p.get("caption") or "", p.get("photo")))
            res = {"message_id": len(self.out), "date": 0, "chat": {"id": ADMIN, "type": "private"}, "text": "x"}

        class R:
            status_code = 200
            text = json.dumps({"ok": True, "result": res})

            def json(self):
                return json.loads(self.text)
        return R()

    def wait_for(self, needle, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                for o in self.out:
                    if needle in o[2]:
                        return o
            time.sleep(0.05)
        raise AssertionError(f"не дождались сообщения с «{needle}». Было: {[o[2][:60] for o in self.out]}")


@pytest.fixture
def running_bot(tmp_path, monkeypatch):
    from config.settings import settings
    from telegram_bot import bot as botmod

    monkeypatch.setattr(settings, "telegram_bot_token", "123456:E2E_TEST_TOKEN")
    monkeypatch.setattr(settings, "admin_ids", [ADMIN])
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "health_enabled", False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "e2e.db"))
    monkeypatch.setattr("telegram_bot.monitor.random.uniform", lambda a, b: 0)

    pages = {"html": mfe_page([mfe_item(7800000001, "Старый велосипед", NOW_MS - 3_600_000)])}
    monkeypatch.setattr(avito_fetcher.AvitoFetcher, "fetch",
                        lambda self, url: avito_fetcher.FetchResult(avito_fetcher.OK, html=pages["html"], method="e2e"))

    tg = FakeTelegram()
    apihelper.CUSTOM_REQUEST_SENDER = tg
    bots = []
    orig = telebot.TeleBot.__init__

    def capture(self, *a, **k):
        orig(self, *a, **k)
        bots.append(self)
    monkeypatch.setattr(telebot.TeleBot, "__init__", capture)

    t = threading.Thread(target=botmod.main, daemon=True)
    t.start()
    end = time.time() + 10
    while not (botmod._State.monitor and botmod._State.monitor.is_alive()) and time.time() < end:
        time.sleep(0.05)
    yield tg, pages, botmod
    botmod._State.monitor.stop()
    for b in bots:
        b.stop_polling()
    time.sleep(0.3)
    apihelper.CUSTOM_REQUEST_SENDER = None


def test_user_journey(running_bot):
    tg, pages, botmod = running_bot

    tg.user_sends("/start", user=999)                     # посторонний
    tg.wait_for("личный бот")

    tg.user_sends("/start")
    tg.wait_for("Здравствуйте, Иван")

    tg.user_sends(data="add")
    tg.wait_for("Пришлите ссылку")
    tg.user_sends("https://www.avito.ru/moskva/velosipedy?q=stels&pmax=20000")
    tg.wait_for("добавлен")
    tg.wait_for("подключён")                               # первая проверка: запомнил текущие

    # Появились новые объявления
    pages["html"] = mfe_page([
        mfe_item(7800000003, "Новый Stels", NOW_MS - 60_000),
        mfe_item(7800000002, "Почти новый Stels", NOW_MS - 600_000),
        mfe_item(7800000001, "Старый велосипед", NOW_MS - 3_600_000),
    ])
    tg.user_sends(data="check_all")
    first = tg.wait_for("Новый Stels")
    tg.wait_for("Почти новый Stels")
    assert first[0] == "sendPhoto" and "НОВОЕ ОБЪЯВЛЕНИЕ AVITO" in first[2]
    assert "Опубликовано только что" in first[2]
    photos = [o for o in tg.out if o[0] == "sendPhoto"]
    assert [("Новый Stels" in o[2]) for o in photos] == [True, False]   # свежее первым

    # Повторная проверка — без повторов
    n_before = len([o for o in tg.out if o[0] == "sendPhoto"])
    tg.user_sends(data="check_all")
    time.sleep(1.5)
    assert len([o for o in tg.out if o[0] == "sendPhoto"]) == n_before

    tg.user_sends("/status")
    status = tg.wait_for("Статистика и состояние")[2]
    assert "Уведомлений отправлено: <b>2</b>" in status and "Активных поисков: <b>1</b>" in status

    tg.user_sends("/pause")
    tg.wait_for("Мониторинг приостановлен")
    tg.user_sends("/resume")
    tg.wait_for("Мониторинг возобновлён")
