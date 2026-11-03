"""Финальные проверки перед запуском: доступ, блокировки в живом потоке, путь к базе."""
import importlib
import time

from database.monitor_db import MonitorDB
from services.avito_fetcher import BLOCKED, OK
from telegram_bot.monitor import Monitor
from tests.fixtures import card, page
from tests.test_bot import ADMIN, STRANGER, env, msg, press  # noqa: F401  (фикстура env)
from tests.test_monitor import FakeFetcher, FakeNotifier

ALL_COMMANDS = ["/start", "/help", "/add", "/add https://www.avito.ru/moskva/velosipedy", "/list", "/remove",
                "/pause", "/resume", "/status", "/interval", "/interval 5", "https://www.avito.ru/moskva/velosipedy",
                "привет"]
ALL_BUTTONS = ["menu", "add", "cancel", "list", "remove", "del:1", "delok:1", "toggle:1", "check:1",
               "check_all", "pause", "resume", "status", "settings", "interval", "setint:60", "photos", "journal:0"]


def test_stranger_cannot_do_anything(env):  # noqa: F811
    bot, db, mon, cap = env
    sid = db.add_search(ADMIN, "https://www.avito.ru/moskva/velosipedy?s=104", "S")
    before = (mon.paused, mon.interval_minutes, db.get("send_photos"))
    for text in ALL_COMMANDS:
        msg(bot, text, user=STRANGER)
        assert "личный бот" in cap.last_text(), text
    n_texts = len(cap.texts())
    for data in ALL_BUTTONS:
        press(bot, data.replace(":1", f":{sid}"), user=STRANGER)
    assert len(cap.texts()) == n_texts  # на кнопки посторонним — только всплывающее «Доступ закрыт»
    assert all(p.get("text") == "⛔ Доступ закрыт" for n, p in cap.calls[-len(ALL_BUTTONS):])
    # Ничего не изменилось
    assert (mon.paused, mon.interval_minutes, db.get("send_photos")) == before
    assert len(db.list_searches()) == 1 and db.get_search(sid)["is_active"] == 1
    assert db.list_searches(STRANGER) == []


def test_admin_id_parsing(monkeypatch):
    from config import settings as st
    for raw, expected in [("123456789", [123456789]), (" 123456789 ", [123456789]),
                          ("111, 222", [111, 222]), ("ID: 555", [555]), ("", [])]:
        monkeypatch.setenv("TELEGRAM_ADMIN_ID", raw)
        assert st._env_ids("TELEGRAM_ADMIN_ID") == expected, raw


def test_check_button_during_block_explains_pause(env):  # noqa: F811
    bot, db, mon, cap = env
    db.add_search(ADMIN, "u", "S")
    db.set("blocked_until", int(time.time()) + 900)
    press(bot, "check_all")
    alert = [p for n, p in cap.calls if n == "answerCallbackQuery"][-1]
    assert "на паузе до" in alert["text"] and not mon._force_all


def test_live_thread_survives_block_and_resumes(tmp_path, monkeypatch):
    """Настоящий фоновый поток: блокировка -> пауза -> автоматическое возобновление без вмешательства."""
    from config.settings import settings
    monkeypatch.setattr(settings, "block_pause_minutes", 0)   # пауза 0 мин, чтобы тест шёл секунды
    db = MonitorDB(str(tmp_path / "live.db"))
    fetcher, notifier = FakeFetcher(), FakeNotifier()
    fetcher.status = BLOCKED
    mon = Monitor(db, notifier, fetcher=fetcher, admin_ids=[ADMIN], pause_between_searches=None,
                  sleep=lambda s: None)
    db.add_search(ADMIN, "u", "S")
    db.set("next_check_at", int(time.time()))
    mon.start()
    try:
        end = time.time() + 10
        while not any("ограничил" in t for _, t in notifier.texts) and time.time() < end:
            time.sleep(0.05)
        assert any("ограничил" in t for _, t in notifier.texts)
        fetcher.status = OK
        fetcher.html = page([card(9100001, "A")])
        db.set("next_check_at", int(time.time()))
        mon.check_now()
        end = time.time() + 10
        while not any("подключён" in t for _, t in notifier.texts) and time.time() < end:
            time.sleep(0.05)
        assert any("восстановлен" in t for _, t in notifier.texts)
        assert any("подключён" in t for _, t in notifier.texts)  # первый успешный заход = запоминание
        assert notifier.listings == []                           # старые объявления не отправлены
        assert mon.is_alive() and time.time() - mon.heartbeat < 60
    finally:
        mon.stop()


def test_database_path_is_persistent_volume(monkeypatch):
    """В Docker DATA_DIR=/data (Dockerfile ENV) -> база /data/avito_bot.db, логи /data/logs."""
    monkeypatch.setenv("DATA_DIR", "/data")
    monkeypatch.delenv("DB_PATH", raising=False)
    from config import settings as st
    s = st.Settings()
    assert s.db_path == "/data/avito_bot.db" and s.logs_dir == "/data/logs"
    dockerfile = open("Dockerfile", encoding="utf-8").read()
    assert "DATA_DIR=/data" in dockerfile and 'CMD ["python", "-m", "telegram_bot.bot"]' in dockerfile
    amvera = open("amvera.yml", encoding="utf-8").read()
    assert "persistenceMount: /data" in amvera and "containerPort: 8080" in amvera


def test_first_add_sends_no_old_listings_even_after_block(tmp_path):
    db = MonitorDB(str(tmp_path / "b.db"))
    f, n = FakeFetcher(), FakeNotifier()
    m = Monitor(db, n, fetcher=f, admin_ids=[ADMIN], pause_between_searches=None, sleep=lambda s: None)
    sid = db.add_search(ADMIN, "u", "S")
    f.status = BLOCKED
    m.check_search(db.get_search(sid))          # первая проверка не удалась
    db.set("blocked_until", 0)
    f.status = OK
    f.html = page([card(9200000 + i, f"Old{i}") for i in range(30)])
    m.run_cycle()                                # первая удачная — только запоминание
    m.run_cycle()
    assert n.listings == []
    assert db.get_search(sid)["baseline_done"] == 1
    # Перезапуск: поиск и история на месте
    db2 = MonitorDB(db.db_path)
    assert db2.get_search(sid)["url"] == "u" and db2.stats(ADMIN)["items_total"] == 30
