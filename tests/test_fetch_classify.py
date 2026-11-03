"""Классификация ответов Avito и повтор при временных сетевых сбоях."""
import requests

from services import avito_fetcher as af
from services.avito_fetcher import (BLOCKED, EMPTY, ERROR, OK, REASON_CAPTCHA,
                                     REASON_FORBIDDEN, REASON_RATE_LIMIT, AvitoFetcher, block_reason)
from tests.fixtures import card, page


class FakeResp:
    def __init__(self, status, text, url="https://www.avito.ru/x"):
        self.status_code, self.text, self.url = status, text, url
        self.content = text.encode()
        self.history = []


def make_fetcher(monkeypatch, responses):
    """responses — список ответов/исключений, выдаются по очереди на каждый session.get."""
    f = AvitoFetcher("http")
    seq = list(responses)
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(f.session, "get", fake_get)
    return f, calls


def test_block_reason_classification():
    assert block_reason("<title>Доступ ограничен: проблема с IP</title>", 403) == REASON_FORBIDDEN
    assert block_reason("", 429) == REASON_RATE_LIMIT
    assert block_reason("", 439) == REASON_RATE_LIMIT
    assert block_reason("<div>geetest captcha</div>", 200) == REASON_CAPTCHA
    assert block_reason(page([card(1, "A")]), 200) is None          # нормальный каталог
    assert block_reason("<html>ничего</html>", 200) is None


def test_http_403_is_forbidden(monkeypatch):
    f, _ = make_fetcher(monkeypatch, [FakeResp(403, "<title>Доступ ограничен: проблема с IP</title>")])
    r = f.fetch("u")
    assert r.status == BLOCKED and r.block_reason == REASON_FORBIDDEN and "403" in r.error


def test_http_429_is_rate_limit(monkeypatch):
    f, _ = make_fetcher(monkeypatch, [FakeResp(429, "Too many requests")])
    r = f.fetch("u")
    assert r.status == BLOCKED and r.block_reason == REASON_RATE_LIMIT


def test_captcha_detected(monkeypatch):
    f, _ = make_fetcher(monkeypatch, [FakeResp(200, "<html><body>geetest hcaptcha</body></html>")])
    r = f.fetch("u")
    assert r.status == BLOCKED and r.block_reason == REASON_CAPTCHA


def test_empty_result_is_not_error(monkeypatch):
    html = "<html><div data-marker='catalog-serp'></div>По вашему запросу ничего не найдено</html>"
    f, _ = make_fetcher(monkeypatch, [FakeResp(200, html)])
    r = f.fetch("u")
    assert r.status == EMPTY and r.error is None


def test_ok_catalog(monkeypatch):
    f, _ = make_fetcher(monkeypatch, [FakeResp(200, page([card(1000001, "A")]))])
    r = f.fetch("u")
    assert r.status == OK and r.html


def test_transient_network_error_retried_once(monkeypatch):
    monkeypatch.setattr(af.time, "sleep", lambda s: None)
    # Первый запрос — сброс соединения, второй — успех. Ровно 2 попытки.
    f, calls = make_fetcher(monkeypatch, [requests.ConnectionError("reset"),
                                          FakeResp(200, page([card(1000001, "A")]))])
    r = f.fetch("u")
    assert r.status == OK and r.attempts == 2 and calls["n"] == 2


def test_transient_error_twice_gives_error_no_more_than_two_tries(monkeypatch):
    monkeypatch.setattr(af.time, "sleep", lambda s: None)
    f, calls = make_fetcher(monkeypatch, [requests.Timeout("t1"), requests.Timeout("t2")])
    r = f.fetch("u")
    assert r.status == ERROR and calls["n"] == 2 and "Timeout" in r.error


def test_block_is_not_retried(monkeypatch):
    monkeypatch.setattr(af.time, "sleep", lambda s: None)
    f, calls = make_fetcher(monkeypatch, [FakeResp(429, "blocked")])
    r = f.fetch("u")
    assert r.status == BLOCKED and calls["n"] == 1    # без повторов при блокировке


def test_404_is_error_not_block(monkeypatch):
    f, _ = make_fetcher(monkeypatch, [FakeResp(404, "not found")])
    r = f.fetch("u")
    assert r.status == ERROR and r.http_status == 404
