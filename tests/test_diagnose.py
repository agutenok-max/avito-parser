"""Временная команда /diagnose."""
import time

import pytest
import requests

from services import diagnostics
from telegram_bot.handlers import diagnose_text
from tests.fixtures import BLOCK_PAGE, card, page
from tests.test_bot import ADMIN, STRANGER, env, msg  # noqa: F401


class FakeResp:
    def __init__(self, url, status, text, history=0, json_data=None):
        self.url, self.status_code, self.text = url, status, text
        self.content = text.encode()
        self.history = [object()] * history
        self._json = json_data

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = responses, []
        self.trust_env, self.headers, self.proxies = True, {}, {}

    def get(self, url, **kw):
        self.calls.append(url)
        r = self.responses[url]
        if isinstance(r, Exception):
            raise r
        return r

    def close(self):
        pass


SEARCH = "https://www.avito.ru/moskva/velosipedy?q=bmx&s=104"


def install(monkeypatch, responses):
    sess = FakeSession(responses)
    orig = diagnostics._session

    def factory():
        real = orig()                      # проверяем настройки настоящей сессии
        sess.trust_env, sess.headers, sess.proxies = real.trust_env, real.headers, real.proxies
        return sess
    monkeypatch.setattr(diagnostics, "_session", factory)
    return sess


def test_blocked_by_ip(monkeypatch):
    sess = install(monkeypatch, {
        diagnostics.IP_SERVICE: FakeResp(diagnostics.IP_SERVICE, 200, "{}",
                                         json_data={"ip": "203.0.113.7", "country": "RU", "city": "Moscow",
                                                    "org": "AS0000 Example Hosting"}),
        diagnostics.AVITO_HOME: FakeResp("https://www.avito.ru/", 429, BLOCK_PAGE),
        SEARCH: FakeResp("https://www.avito.ru/moskva/velosipedy?q=bmx&s=104&sessid=SECRET", 429, BLOCK_PAGE),
    })
    res = diagnostics.run_diagnostics(SEARCH)
    # Ровно по одному запросу, без повторов
    assert sess.calls == [diagnostics.IP_SERVICE, diagnostics.AVITO_HOME, SEARCH]
    assert sess.trust_env is False and sess.proxies == {}
    text = diagnose_text(res)
    assert "203.0.113.7" in text and "Moscow, RU" in text and "Example Hosting" in text
    assert "HTTP-статус: <b>429</b>" in text
    assert "Признаки CAPTCHA/проверки робота: <b>да</b>" in text and "captcha" in text
    assert "Avito ограничивает доступ" in text
    assert "SECRET" not in text and "sessid" not in text      # параметры конечного URL скрыты
    assert "Время ответа:" in text and "Конечный URL:" in text


def test_ok_with_catalog(monkeypatch):
    install(monkeypatch, {
        diagnostics.IP_SERVICE: FakeResp(diagnostics.IP_SERVICE, 200, "{}", json_data={"ip": "198.51.100.1"}),
        diagnostics.AVITO_HOME: FakeResp("https://www.avito.ru/", 200, "<html><title>Авито</title></html>"),
        SEARCH: FakeResp(SEARCH, 200, page([card(1000001, "A")]), history=1),
    })
    text = diagnose_text(diagnostics.run_diagnostics(SEARCH))
    assert text.count("Признаки CAPTCHA/проверки робота: нет ✅") == 2
    assert "Список объявлений на странице: есть ✅" in text and "редиректов: 1" in text
    assert "Доступ к Avito есть" in text


def test_connection_errors_and_no_search(monkeypatch):
    install(monkeypatch, {
        diagnostics.IP_SERVICE: requests.ConnectionError("dns"),
        diagnostics.AVITO_HOME: requests.Timeout("timed out"),
    })
    text = diagnose_text(diagnostics.run_diagnostics(None))
    assert "Не удалось определить" in text and "Ошибка соединения" in text
    assert "Сохранённых поисков нет" in text


def test_command_admin_only(env, monkeypatch):  # noqa: F811
    bot, db, mon, cap = env
    called = []
    monkeypatch.setattr(diagnostics, "run_diagnostics", lambda url: called.append(url) or {
        "ip": diagnostics.IpInfo(ip="203.0.113.7"), "proxy": False,
        "home": diagnostics.ProbeResult("Главная Avito", diagnostics.AVITO_HOME, status=200, final_url="x",
                                        elapsed=0.1, size=10),
        "search": None})
    msg(bot, "/diagnose", user=STRANGER)
    assert "личный бот" in cap.last_text() and called == []
    db.add_search(ADMIN, SEARCH, "S")
    msg(bot, "/diagnose")
    end = time.time() + 5
    while not any("Диагностика доступа к Avito" in t for t in cap.texts()) and time.time() < end:
        time.sleep(0.05)
    assert called == [SEARCH]
    assert any("203.0.113.7" in t for t in cap.texts())
