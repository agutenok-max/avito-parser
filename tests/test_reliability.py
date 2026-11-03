"""Надёжность: страницы 2+, старые объявления, перезапуск, фото, health-check."""
import json
import time

import telebot
from telebot import apihelper

from database.monitor_db import OLD, MonitorDB
from services.avito_fetcher import OK, FetchResult
from telegram_bot.monitor import Monitor
from tests.fixtures import card, mfe_item, mfe_page, page
from tests.test_monitor import FakeNotifier

NOW_MS = int(time.time() * 1000)


class PagedFetcher:
    mode = "test"

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def fetch(self, url):
        self.urls.append(url)
        n = 1
        if "p=" in url:
            n = int(url.split("p=")[1].split("&")[0])
        return FetchResult(OK, html=self.pages.get(n, page([])), method="test")


def mk(tmp_path, fetcher):
    db = MonitorDB(str(tmp_path / "r.db"))
    n = FakeNotifier()
    m = Monitor(db, n, fetcher=fetcher, admin_ids=[1], pause_between_searches=None, sleep=lambda s: None)
    return db, n, m


def test_second_page_checked_when_whole_first_page_is_new(tmp_path):
    f = PagedFetcher({1: page([card(5000001, "Old")])})
    db, n, m = mk(tmp_path, f)
    db.add_search(1, "https://www.avito.ru/moskva/velosipedy?s=104", "S")
    m.run_cycle()  # baseline
    # После долгой паузы вся первая страница новая — монитор заглядывает на вторую
    f.pages = {1: page([card(5000010 + i, f"N{i}", date="2 минуты назад") for i in range(3)]),
               2: page([card(5000020, "P2", date="20 минут назад"), card(5000001, "Old")])}
    m.run_cycle()
    assert any("p=2" in u for u in f.urls)
    assert sorted(i for _, i in n.listings) == ["5000010", "5000011", "5000012", "5000020"]


def test_old_promoted_listing_is_not_sent(tmp_path):
    f = PagedFetcher({1: mfe_page([mfe_item(7700000001, "A", NOW_MS - 60_000)])})
    db, n, m = mk(tmp_path, f)
    sid = db.add_search(1, "u", "S")
    m.run_cycle()
    f.pages = {1: mfe_page([
        mfe_item(7700000002, "Fresh", NOW_MS - 30_000),
        mfe_item(7700000003, "Old promoted", NOW_MS - 20 * 86400 * 1000, promoted=True),
        mfe_item(7700000001, "A", NOW_MS - 60_000),
    ])}
    m.run_cycle()
    assert [i for _, i in n.listings] == ["7700000002"]
    statuses = {r["item_id"]: r["status"] for r in db.recent_items(1, 50)}
    assert "7700000003" not in statuses  # в журнал «новых» не попадает
    m.run_cycle()
    assert len(n.listings) == 1
    assert db.stats(1)["items_total"] == 3


def test_restart_does_not_resend_and_db_survives(tmp_path):
    f = PagedFetcher({1: page([card(6000001, "A")])})
    db, n, m = mk(tmp_path, f)
    db.add_search(1, "u", "S")
    m.run_cycle()
    f.pages = {1: page([card(6000002, "B"), card(6000001, "A")])}
    m.run_cycle()
    assert len(n.listings) == 1
    for _ in range(3):  # три «перезапуска»
        db2 = MonitorDB(db.db_path)
        n2 = FakeNotifier()
        Monitor(db2, n2, fetcher=f, admin_ids=[1], pause_between_searches=None, sleep=lambda s: None).run_cycle()
        assert n2.listings == []
    assert MonitorDB(db.db_path).stats(1)["notifications_sent"] == 1


def test_photo_fallbacks(tmp_path, monkeypatch):
    from telegram_bot import bot as botmod
    calls = []

    def sender(method, url, **kw):
        name = url.rsplit("/", 1)[-1]
        calls.append((name, kw.get("files"), (kw.get("params") or {}).get("photo")))
        if name == "sendPhoto" and kw.get("files") is None:
            body = {"ok": False, "error_code": 400, "description": "Bad Request: wrong file identifier/HTTP URL"}
            code = 400
        else:
            body = {"ok": True, "result": {"message_id": 77, "date": 0, "chat": {"id": 1, "type": "private"}}}
            code = 200

        class R:
            status_code = code
            text = json.dumps(body)
            reason = "x"

            def json(self):
                return body
        return R()

    apihelper.CUSTOM_REQUEST_SENDER = sender
    try:
        b = telebot.TeleBot("123456:TEST", parse_mode="HTML", threaded=False)
        db = MonitorDB(str(tmp_path / "p.db"))
        notifier = botmod.TelegramNotifier(b, db)
        item = {"title": "T", "url": "https://www.avito.ru/x_1", "images": ["https://img/x.jpg"],
                "price": "1 ₽", "address": "M", "description": "d", "published_ts": None, "published_text": None}
        # Фото по ссылке не прошло -> скачали сами и загрузили файлом
        monkeypatch.setattr(botmod.TelegramNotifier, "_download_image", staticmethod(lambda u: b"\x89PNG..."))
        assert notifier.send_listing(1, item, {"title": "S"}) == 77
        assert [c[0] for c in calls] == ["sendPhoto", "sendPhoto"] and calls[1][1] is not None
        # Скачать тоже не вышло -> текстовое сообщение
        calls.clear()
        monkeypatch.setattr(botmod.TelegramNotifier, "_download_image", staticmethod(lambda u: None))
        assert notifier.send_listing(1, item, {"title": "S"}) == 77
        assert [c[0] for c in calls] == ["sendPhoto", "sendMessage"]
        # Фото выключены в настройках -> сразу текст
        calls.clear()
        db.set("send_photos", False)
        notifier.send_listing(1, item, {"title": "S"})
        assert [c[0] for c in calls] == ["sendMessage"]
    finally:
        apihelper.CUSTOM_REQUEST_SENDER = None


def test_health_payload(tmp_path):
    from telegram_bot import bot as botmod
    db = MonitorDB(str(tmp_path / "h.db"))
    m = Monitor(db, FakeNotifier(), fetcher=PagedFetcher({}), admin_ids=[1])
    botmod._State.monitor = m
    code, body = botmod.health_payload()
    assert code == 503 and body["status"] == "degraded"  # поток ещё не запущен
    m.start()
    try:
        code, body = botmod.health_payload()
        assert code == 200 and body["status"] == "ok" and body["monitor_alive"]
    finally:
        m.stop()
        botmod._State.monitor = None


def test_storage_check(tmp_path, monkeypatch):
    from config.settings import settings
    from telegram_bot import bot as botmod
    monkeypatch.setattr(settings, "data_dir", str(tmp_path / "data"))
    info = botmod.check_storage()
    assert info["writable"] and info["persistent"] is None


def test_journal_mode_default_delete(tmp_path):
    import sqlite3
    db = MonitorDB(str(tmp_path / "j.db"))
    db.set("x", 1)
    mode = sqlite3.connect(db.db_path).execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "delete"
