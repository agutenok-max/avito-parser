from database.models import Listing
from database.monitor_db import BASELINE, MonitorDB, PENDING, SENT


def L(i):
    return Listing(url=f"https://www.avito.ru/x_{i}", title=f"T{i}", item_id=str(i))


def test_dedupe_and_persistence(tmp_path):
    path = str(tmp_path / "db.sqlite")
    db = MonitorDB(path)
    sid = db.add_search(1, "https://www.avito.ru/a?s=104", "A")
    assert db.add_search(1, "https://www.avito.ru/a?s=104", "A") is None  # дубликат поиска
    base = db.save_new_items(sid, [L(1), L(2)], baseline=True)
    assert len(base) == 2 and all(r["status"] == BASELINE for r in base)
    new = db.save_new_items(sid, [L(1), L(2), L(3)])
    assert [r["item_id"] for r in new] == ["3"] and new[0]["status"] == PENDING
    assert db.save_new_items(sid, [L(3)]) == []
    db.mark_sent(new[0]["id"], sid, 1, 42)

    db2 = MonitorDB(path)  # «перезапуск сервера»
    assert db2.save_new_items(sid, [L(1), L(2), L(3)]) == []
    assert db2.get_item(new[0]["id"])["status"] == SENT
    st = db2.stats(1)
    assert st["items_total"] == 3 and st["items_new"] == 1 and st["notifications_sent"] == 1
    assert db2.get_search(sid)["items_found"] == 1


def test_send_failures_and_delete(tmp_path):
    db = MonitorDB(str(tmp_path / "db.sqlite"))
    sid = db.add_search(1, "u", "A")
    row = db.save_new_items(sid, [L(5)])[0]
    for _ in range(3):
        db.mark_send_failed(row["id"])
    assert db.pending_items(sid) == []
    db.set("interval_minutes", 7)
    assert db.get("interval_minutes") == 7
    assert db.delete_search(sid)
    assert db.get_item(row["id"]) is None
