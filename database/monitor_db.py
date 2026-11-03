"""
База данных мониторинга (SQLite).

Хранит пользователей, поиски, найденные объявления, историю уведомлений
и настройки. Защита от дубликатов — уникальный ключ (search_id, item_id):
одно и то же объявление в рамках одного поиска записывается и отправляется
только один раз, даже после перезапуска.
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from database.models import Listing

# Статусы уведомления по объявлению
PENDING = 0    # найдено, ещё не отправлено
SENT = 1       # уведомление отправлено
BASELINE = 2   # было на странице при добавлении поиска — не отправляем
SKIPPED = 3    # не отправлено (превышен лимит за проверку / ошибки отправки)

MAX_SEND_ATTEMPTS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL,
    url                TEXT NOT NULL,
    title              TEXT NOT NULL,
    is_active          INTEGER NOT NULL DEFAULT 1,
    baseline_done      INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL,
    last_check_at      INTEGER,
    last_success_at    INTEGER,
    last_error         TEXT,
    items_found        INTEGER NOT NULL DEFAULT 0,
    notifications_sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE(user_id, url)
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id       INTEGER NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
    item_id         TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    price           TEXT,
    address         TEXT,
    description     TEXT,
    images          TEXT,
    published_ts    INTEGER,
    published_text  TEXT,
    found_at        INTEGER NOT NULL,
    status          INTEGER NOT NULL DEFAULT 0,
    send_attempts   INTEGER NOT NULL DEFAULT 0,
    notified_at     INTEGER,
    UNIQUE(search_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, search_id);
CREATE INDEX IF NOT EXISTS idx_items_found ON items(found_at);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_row_id INTEGER NOT NULL,
    search_id   INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    sent_at     INTEGER NOT NULL,
    UNIQUE(item_row_id, chat_id)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class MonitorDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # --------------------------------------------------------- connection
    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA synchronous = NORMAL")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # --------------------------------------------------------------- kv
    def get(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except Exception:
            return row["value"]

    def set(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def incr(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self.set(key, int(self.get(key, 0) or 0) + amount)

    # ------------------------------------------------------------ users
    def upsert_user(self, user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO users(user_id, username, first_name, created_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, first_name = excluded.first_name",
                (user_id, username, first_name, int(time.time())),
            )

    # --------------------------------------------------------- searches
    def add_search(self, user_id: int, url: str, title: str) -> Optional[int]:
        """Добавляет поиск. Возвращает id или None, если такой поиск уже есть."""
        with self._conn() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO searches(user_id, url, title, created_at) VALUES(?, ?, ?, ?)",
                    (user_id, url, title, int(time.time())),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def get_search(self, search_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        return dict(row) if row else None

    def list_searches(self, user_id: Optional[int] = None, only_active: bool = False) -> List[Dict]:
        sql = "SELECT * FROM searches WHERE 1=1"
        params: list = []
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        if only_active:
            sql += " AND is_active = 1"
        sql += " ORDER BY id"
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def count_searches(self, user_id: Optional[int] = None) -> int:
        return len(self.list_searches(user_id))

    def set_search_active(self, search_id: int, active: bool) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE searches SET is_active = ? WHERE id = ?", (1 if active else 0, search_id))

    def delete_search(self, search_id: int) -> bool:
        with self._conn() as conn:
            conn.execute("DELETE FROM notifications WHERE search_id = ?", (search_id,))
            cur = conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
            return cur.rowcount > 0

    def mark_check(self, search_id: int, ok: bool, error: Optional[str] = None) -> None:
        now = int(time.time())
        with self._conn() as conn:
            if ok:
                conn.execute(
                    "UPDATE searches SET last_check_at = ?, last_success_at = ?, last_error = NULL WHERE id = ?",
                    (now, now, search_id),
                )
            else:
                conn.execute(
                    "UPDATE searches SET last_check_at = ?, last_error = ? WHERE id = ?",
                    (now, (error or "")[:300], search_id),
                )

    # ------------------------------------------------------------ items
    def save_new_items(self, search_id: int, listings: List[Listing], baseline: bool = False) -> List[Dict]:
        """
        Записывает объявления, которых ещё нет у этого поиска.
        Возвращает только действительно новые записи (для отправки).
        Если baseline=True — записи помечаются как «уже были» и не отправляются.
        """
        now = int(time.time())
        new_rows: List[Dict] = []
        with self._conn() as conn:
            for l in listings:
                if not l.item_id:
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO items(search_id, item_id, url, title, price, address, description, "
                    "images, published_ts, published_text, found_at, status) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (search_id, l.item_id, l.url, l.title, l.price, l.address, l.description,
                     json.dumps(l.images or []), l.published_ts, l.published_text, now,
                     BASELINE if baseline else PENDING),
                )
                if cur.rowcount:
                    row = conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()
                    new_rows.append(self._item(row))
            if new_rows and not baseline:
                conn.execute(
                    "UPDATE searches SET items_found = items_found + ? WHERE id = ?",
                    (len(new_rows), search_id),
                )
            if baseline:
                conn.execute("UPDATE searches SET baseline_done = 1 WHERE id = ?", (search_id,))
        return new_rows

    def pending_items(self, search_id: int) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM items WHERE search_id = ? AND status = ? AND send_attempts < ?",
                (search_id, PENDING, MAX_SEND_ATTEMPTS),
            ).fetchall()
        return [self._item(r) for r in rows]

    def mark_sent(self, item_row_id: int, search_id: int, chat_id: int, message_id: Optional[int]) -> None:
        now = int(time.time())
        with self._conn() as conn:
            conn.execute("UPDATE items SET status = ?, notified_at = ? WHERE id = ?", (SENT, now, item_row_id))
            conn.execute(
                "INSERT OR IGNORE INTO notifications(item_row_id, search_id, chat_id, message_id, sent_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (item_row_id, search_id, chat_id, message_id, now),
            )
            conn.execute(
                "UPDATE searches SET notifications_sent = notifications_sent + 1 WHERE id = ?", (search_id,)
            )

    def mark_send_failed(self, item_row_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE items SET send_attempts = send_attempts + 1 WHERE id = ?", (item_row_id,))
            conn.execute(
                "UPDATE items SET status = ? WHERE id = ? AND send_attempts >= ?",
                (SKIPPED, item_row_id, MAX_SEND_ATTEMPTS),
            )

    def mark_skipped(self, item_row_ids: List[int]) -> None:
        if not item_row_ids:
            return
        with self._conn() as conn:
            conn.executemany("UPDATE items SET status = ? WHERE id = ?", [(SKIPPED, i) for i in item_row_ids])

    def recent_items(self, user_id: int, limit: int = 10, offset: int = 0) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT i.*, s.title AS search_title FROM items i JOIN searches s ON s.id = i.search_id "
                "WHERE s.user_id = ? AND i.status IN (?, ?, ?) "
                "ORDER BY i.found_at DESC, COALESCE(i.published_ts, 0) DESC, i.id DESC LIMIT ? OFFSET ?",
                (user_id, SENT, PENDING, SKIPPED, limit, offset),
            ).fetchall()
        return [self._item(r) for r in rows]

    def get_item(self, item_row_id: int) -> Optional[Dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM items WHERE id = ?", (item_row_id,)).fetchone()
        return self._item(row) if row else None

    # ------------------------------------------------------------ stats
    def stats(self, user_id: Optional[int] = None) -> Dict[str, int]:
        where, params = ("WHERE s.user_id = ?", [user_id]) if user_id is not None else ("", [])
        with self._conn() as conn:
            s = conn.execute(
                f"SELECT COUNT(*) AS total, COALESCE(SUM(is_active), 0) AS active FROM searches s {where}", params
            ).fetchone()
            i = conn.execute(
                f"SELECT COUNT(*) AS total, "
                f"COALESCE(SUM(CASE WHEN i.status != {BASELINE} THEN 1 ELSE 0 END), 0) AS new_found, "
                f"COALESCE(SUM(CASE WHEN i.found_at > ? THEN 1 ELSE 0 END), 0) AS last_day "
                f"FROM items i JOIN searches s ON s.id = i.search_id {where}",
                [int(time.time()) - 86400] + params,
            ).fetchone()
            n = conn.execute(
                f"SELECT COUNT(*) AS total FROM notifications n JOIN searches s ON s.id = n.search_id {where}",
                params,
            ).fetchone()
        return {
            "searches_total": s["total"],
            "searches_active": s["active"],
            "items_total": i["total"],
            "items_new": i["new_found"],
            "items_last_day": i["last_day"],
            "notifications_sent": n["total"],
        }

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _item(row) -> Dict:
        d = dict(row)
        try:
            d["images"] = json.loads(d.get("images") or "[]")
        except Exception:
            d["images"] = []
        return d
