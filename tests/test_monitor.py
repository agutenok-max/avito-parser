import time

from database.monitor_db import MonitorDB
from services.avito_fetcher import BLOCKED, ERROR, OK, FetchResult, looks_blocked
from telegram_bot.monitor import Monitor, Notifier, sort_freshest_first
from tests.fixtures import BLOCK_PAGE, card, page


class FakeFetcher:
    mode = "test"

    def __init__(self):
        self.html = page([])
        self.status = OK
        self.calls = 0

    def fetch(self, url):
        self.calls += 1
        if self.status == OK:
            return FetchResult(OK, html=self.html, method="test")
        return FetchResult(self.status, error="Avito ограничил доступ (HTTP 429)" if self.status == BLOCKED else "x")


class FakeNotifier(Notifier):
    def __init__(self):
        self.listings, self.texts = [], []
        self.fail = False

    def send_listing(self, chat_id, item, search):
        if self.fail:
            raise RuntimeError("telegram down")
        self.listings.append((chat_id, item["item_id"]))
        return len(self.listings)

    def send_text(self, chat_id, text, url_button=None):
        self.texts.append((chat_id, text))


def make(tmp_path):
    db = MonitorDB(str(tmp_path / "m.db"))
    fetcher, notifier = FakeFetcher(), FakeNotifier()
    mon = Monitor(db, notifier, fetcher=fetcher, admin_ids=[111], pause_between_searches=None,
                  sleep=lambda s: None)
    return db, fetcher, notifier, mon


def test_full_flow_only_new_and_no_duplicates(tmp_path):
    db, fetcher, notifier, mon = make(tmp_path)
    sid = db.add_search(111, "https://www.avito.ru/moskva/velosipedy?s=104", "Велосипеды")

    fetcher.html = page([card(2000001, "A"), card(2000002, "B")])
    mon.run_cycle()  # первая проверка — запоминаем текущие, ничего не шлём
    assert notifier.listings == []
    assert "подключён" in notifier.texts[-1][1]

    fetcher.html = page([card(2000004, "D", date="1 минуту назад"), card(2000003, "C", date="10 минут назад"),
                         card(2000001, "A"), card(2000002, "B")])
    mon.run_cycle()
    assert notifier.listings == [(111, "2000004"), (111, "2000003")]  # свежие первыми

    mon.run_cycle()  # та же страница — повторов нет
    assert len(notifier.listings) == 2

    # «Перезапуск сервера»: новый объект БД и монитора на том же файле
    db2 = MonitorDB(db.db_path)
    mon2 = Monitor(db2, notifier, fetcher=fetcher, admin_ids=[111], pause_between_searches=None,
                   sleep=lambda s: None)
    mon2.run_cycle()
    assert len(notifier.listings) == 2
    st = mon2.status(111)
    assert st["notifications_sent"] == 2 and st["items_new"] == 2 and st["searches_active"] == 1
    assert st["last_check_at"] and st["next_check_at"] > time.time()
    assert db2.get_search(sid)["last_error"] is None


def test_limit_per_check(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "max_notifications_per_check", 2)
    db, fetcher, notifier, mon = make(tmp_path)
    db.add_search(111, "u", "S")
    mon.run_cycle()
    fetcher.html = page([card(3000000 + i, f"N{i}") for i in range(5)])
    mon.run_cycle()
    assert len(notifier.listings) == 2
    assert "ещё 3" in notifier.texts[-1][1]
    mon.run_cycle()
    assert len(notifier.listings) == 2


def test_block_backoff_and_recovery(tmp_path):
    db, fetcher, notifier, mon = make(tmp_path)
    db.add_search(111, "u1", "S1")
    db.add_search(111, "u2", "S2")
    fetcher.status = BLOCKED
    summary = mon.run_cycle()
    assert summary["blocked"] == 1 and fetcher.calls == 1  # после блокировки дальше не стучимся
    assert db.get("blocked_until") > time.time() + 10 * 60
    assert any("ограничил доступ" in t for _, t in notifier.texts)
    assert mon._seconds_to_next() > 0

    db.set("blocked_until", 0)
    fetcher.status = OK
    mon.run_cycle()
    assert any("восстановлен" in t for _, t in notifier.texts)
    assert db.get("block_level") == 0


def test_send_failure_is_retried_later(tmp_path):
    db, fetcher, notifier, mon = make(tmp_path)
    db.add_search(111, "u", "S")
    mon.run_cycle()
    fetcher.html = page([card(4000001, "X")])
    notifier.fail = True
    mon.run_cycle()
    assert notifier.listings == []
    notifier.fail = False
    mon.run_cycle()
    assert notifier.listings == [(111, "4000001")]


def test_pause_and_interval(tmp_path):
    db, fetcher, notifier, mon = make(tmp_path)
    mon.pause()
    assert mon.paused and mon.status()["next_check_at"] is None
    mon.resume()
    assert not mon.paused
    assert mon.set_interval(1) == 2  # минимум
    assert mon.set_interval(10) == 10 and mon.interval_minutes == 10


def test_error_page(tmp_path):
    db, fetcher, notifier, mon = make(tmp_path)
    sid = db.add_search(111, "u", "S")
    fetcher.status = ERROR
    assert mon.run_cycle()["errors"] == 1
    assert db.get_search(sid)["last_error"]


def test_block_detection():
    assert looks_blocked(BLOCK_PAGE, 200)
    assert looks_blocked("", 429)
    assert not looks_blocked(page([card(1, "A")]), 200)


def test_sort_keeps_page_order_when_dates_unknown():
    items = [{"item_id": "1", "published_text": None}, {"item_id": "2", "published_ts": 99}]
    assert [i["item_id"] for i in sort_freshest_first(items)] == ["1", "2"]
