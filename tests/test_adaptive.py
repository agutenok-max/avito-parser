"""Адаптивный интервал и обработка статуса EMPTY в мониторе."""
import time

from services.avito_fetcher import EMPTY, OK, FetchResult
from database.monitor_db import MonitorDB
from telegram_bot.monitor import Monitor
from tests.fixtures import card, page
from tests.test_monitor import FakeNotifier

ADMIN = 1


class SeqFetcher:
    mode = "test"

    def __init__(self):
        self.result = FetchResult(OK, html=page([]), method="test")

    def fetch(self, url):
        return self.result


def mk(tmp_path):
    db = MonitorDB(str(tmp_path / "a.db"))
    f, n = SeqFetcher(), FakeNotifier()
    m = Monitor(db, n, fetcher=f, admin_ids=[ADMIN], pause_between_searches=None, sleep=lambda s: None)
    return db, f, n, m


def test_idle_streak_grows_interval_then_resets(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "adaptive_interval", True)
    monkeypatch.setattr(settings, "adaptive_max_interval_minutes", 20)
    db, f, n, m = mk(tmp_path)
    m.set_interval(3)
    db.add_search(ADMIN, "https://www.avito.ru/moskva/velosipedy?s=104", "S")
    m.run_cycle()  # baseline, new=0 -> idle streak +1
    assert m.effective_interval_minutes == 3  # streak 1 -> factor 1
    for _ in range(5):
        m.run_cycle()   # по-прежнему нет новых
    assert db.get("idle_streak") >= 3
    assert m.effective_interval_minutes >= 6  # вырос, но ограничен 20
    assert m.effective_interval_minutes <= 20

    # Появилось новое объявление -> интервал сбрасывается к базовому
    f.result = FetchResult(OK, html=page([card(9000001, "N", date="1 минуту назад")]), method="test")
    m.run_cycle()
    assert db.get("idle_streak") == 0
    assert m.effective_interval_minutes == 3


def test_adaptive_never_below_base_and_can_be_disabled(tmp_path, monkeypatch):
    from config.settings import settings
    db, f, n, m = mk(tmp_path)
    m.set_interval(5)
    db.set("idle_streak", 99)
    monkeypatch.setattr(settings, "adaptive_interval", True)
    monkeypatch.setattr(settings, "adaptive_max_interval_minutes", 20)
    assert 5 <= m.effective_interval_minutes <= 20
    monkeypatch.setattr(settings, "adaptive_interval", False)
    assert m.effective_interval_minutes == 5  # отключено -> строго базовый


def test_empty_result_is_handled_as_success(tmp_path):
    db, f, n, m = mk(tmp_path)
    sid = db.add_search(ADMIN, "u", "S")
    f.result = FetchResult(EMPTY, html="<html>ничего не найдено</html>", method="test")
    m.run_cycle()                       # baseline на пустой выдаче
    assert db.get_search(sid)["baseline_done"] == 1
    assert db.get_search(sid)["last_error"] is None   # пустая выдача — не ошибка
    r = m.run_cycle()
    assert r["errors"] == 0 and n.listings == []


def test_next_check_uses_effective_interval(tmp_path, monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "adaptive_interval", True)
    monkeypatch.setattr(settings, "adaptive_max_interval_minutes", 20)
    db, f, n, m = mk(tmp_path)
    m.set_interval(3)
    db.add_search(ADMIN, "u", "S")
    db.set("idle_streak", 6)   # factor 3 -> 9 минут
    start = time.time()
    m.run_cycle()
    gap = db.get("next_check_at") - start
    assert 9 * 60 * 0.85 <= gap <= 9 * 60 * 1.15
