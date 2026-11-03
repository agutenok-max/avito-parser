"""
Фоновый мониторинг поисков Avito.

Работает в отдельном потоке, круглосуточно:
  * раз в N минут проверяет все активные поиски;
  * новые объявления (которых ещё нет в базе для этого поиска) отправляет в Telegram;
  * при блокировке со стороны Avito делает паузу (15 → 30 → 60 → 120 минут);
  * любые ошибки логируются, цикл продолжает работу.
"""
import random
import threading
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from config.settings import logger, settings
from database.monitor_db import OLD, MonitorDB
from services.avito_fetcher import BLOCKED, EMPTY, OK, REASON_RATE_LIMIT, AvitoFetcher
from services.avito_processor import AvitoProcessor, parse_relative_date
from services.url_tools import page_url
from telegram_bot.formatting import TZ, esc, fmt_dt


class Notifier:
    """Интерфейс отправки сообщений (реализуется в bot.py, подменяется в тестах)."""

    def send_listing(self, chat_id: int, item: Dict, search: Dict) -> Optional[int]:  # pragma: no cover
        raise NotImplementedError

    def send_text(self, chat_id: int, text: str, url_button: Optional[tuple] = None) -> None:  # pragma: no cover
        raise NotImplementedError


def estimate_ts(item: Dict, now: Optional[datetime] = None) -> Optional[int]:
    """Точное (sortTimeStamp) или примерное («3 часа назад») время публикации."""
    if item.get("published_ts"):
        return int(item["published_ts"])
    if item.get("published_text"):
        now = now or datetime.now(TZ).replace(tzinfo=None)
        dt, _exact = parse_relative_date(item["published_text"], now)
        if dt is not None:
            return int(dt.replace(tzinfo=TZ).timestamp())
    return None


def sort_freshest_first(items: List[Dict], now: Optional[datetime] = None) -> List[Dict]:
    """
    Сортирует объявления: самые свежие первыми.
    Если хотя бы у одного объявления дату определить нельзя — сохраняем порядок
    страницы (ссылка поиска всегда с сортировкой «по дате», s=104).
    """
    now = now or datetime.now(TZ).replace(tzinfo=None)
    keyed = [(estimate_ts(item, now), idx, item) for idx, item in enumerate(items)]
    if all(ts for ts, _, _ in keyed):
        keyed.sort(key=lambda x: (-x[0], x[1]))
    return [item for _, _, item in keyed]


class Monitor:
    def __init__(
        self,
        db: MonitorDB,
        notifier: Notifier,
        fetcher: Optional[AvitoFetcher] = None,
        admin_ids: Optional[List[int]] = None,
        pause_between_searches: tuple = (3.0, 8.0),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.db = db
        self.notifier = notifier
        self.fetcher = fetcher or AvitoFetcher()
        self.admin_ids = admin_ids if admin_ids is not None else settings.admin_ids
        self.pause_between_searches = pause_between_searches
        self._sleep = sleep
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._force: set = set()
        self._force_all = False
        self._cycle_lock = threading.Lock()
        self.heartbeat = time.time()
        self.is_checking = False

    # ----------------------------------------------------------- settings
    @property
    def interval_minutes(self) -> int:
        """Базовый интервал, заданный пользователем (команда /interval)."""
        return int(self.db.get("interval_minutes", settings.check_interval_minutes) or settings.check_interval_minutes)

    @property
    def effective_interval_minutes(self) -> int:
        """
        Фактический интервал до следующей проверки с учётом адаптации.
        Когда новых объявлений долго нет, интервал плавно растёт (меньше нагрузки на Avito),
        но никогда не опускается ниже заданного пользователем и не превышает лимит.
        """
        base = self.interval_minutes
        if not settings.adaptive_interval:
            return base
        streak = int(self.db.get("idle_streak", 0) or 0)
        factor = 1 + min(streak // 3, 2)  # 1x → 2x → 3x
        return max(base, min(base * factor, settings.adaptive_max_interval_minutes))

    def set_interval(self, minutes: int) -> int:
        minutes = max(settings.min_interval_minutes, min(int(minutes), 24 * 60))
        self.db.set("interval_minutes", minutes)
        last = self.db.get("last_check_at")
        self.db.set("next_check_at", int((last or time.time()) + minutes * 60))
        self._wake.set()
        return minutes

    @property
    def paused(self) -> bool:
        return bool(self.db.get("paused", False))

    def pause(self) -> None:
        self.db.set("paused", True)
        self._wake.set()

    def resume(self) -> None:
        self.db.set("paused", False)
        self.db.set("next_check_at", int(time.time()))
        self._wake.set()

    def check_now(self, search_id: Optional[int] = None) -> None:
        if search_id is None:
            self._force_all = True
        else:
            self._force.add(search_id)
        self._wake.set()

    # ------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="avito-monitor", daemon=True)
        self._thread.start()
        logger.info("Мониторинг запущен")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        # После перезапуска сервера продолжаем с того же места
        if not self.db.get("next_check_at"):
            self.db.set("next_check_at", int(time.time()) + 5)
        while not self._stop.is_set():
            self.heartbeat = time.time()
            try:
                self._tick()
            except Exception as e:  # никогда не даём потоку упасть
                logger.exception(f"Ошибка в цикле мониторинга: {e}")
                self._wait(30)
                continue
            self._wait(self._seconds_to_next())

    def _seconds_to_next(self) -> float:
        if self._force or self._force_all:
            return 0
        if self.paused:
            return 30
        now = time.time()
        targets = [self.db.get("next_check_at") or now]
        blocked_until = self.db.get("blocked_until") or 0
        if blocked_until > now:
            targets = [max(targets[0], blocked_until)]
        return max(0.0, min(30.0, min(targets) - now))

    def _wait(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._wake.wait(timeout=seconds)
        self._wake.clear()

    def _tick(self) -> None:
        now = time.time()
        forced_ids = set(self._force)
        force_all = self._force_all
        self._force.clear()
        self._force_all = False

        if forced_ids and not force_all:
            # Ручная проверка отдельных поисков (например, только что добавленного)
            for sid in forced_ids:
                s = self.db.get_search(sid)
                if s:
                    self.check_search(s, notify_errors=True)
            return

        if self.paused and not force_all:
            return
        blocked_until = self.db.get("blocked_until") or 0
        if blocked_until > now and not force_all:
            return
        next_at = self.db.get("next_check_at") or now
        if now < next_at and not force_all:
            return
        self.run_cycle()

    # ------------------------------------------------------------- cycle
    def run_cycle(self) -> Dict[str, int]:
        """Проверяет все активные поиски один раз."""
        with self._cycle_lock:
            self.is_checking = True
            started = time.time()
            summary = {"checked": 0, "new": 0, "errors": 0, "blocked": 0}
            try:
                searches = self.db.list_searches(only_active=True)
                for idx, s in enumerate(searches):
                    if self._stop.is_set():
                        break
                    if idx > 0 and self.pause_between_searches:
                        self._sleep(random.uniform(*self.pause_between_searches))
                    result = self.check_search(s, _in_cycle=True)
                    summary["checked"] += 1
                    if result == BLOCKED:
                        summary["blocked"] += 1
                        break  # дальше не стучимся — ждём окончания паузы
                    if isinstance(result, int):
                        summary["new"] += result
                    elif result != OK:
                        summary["errors"] += 1
            finally:
                self.is_checking = False
                finished = time.time()
                # Адаптация: считаем «простойные» циклы (без новых и без блокировок)
                if summary["checked"]:
                    if summary["new"] > 0 or summary["blocked"]:
                        self.db.set("idle_streak", 0)
                    else:
                        self.db.incr("idle_streak")
                eff_minutes = self.effective_interval_minutes
                interval = eff_minutes * 60
                jitter = random.uniform(-0.1, 0.1) * interval
                self.db.set("last_check_at", int(finished))
                self.db.set("last_cycle_seconds", round(finished - started, 1))
                self.db.set("effective_interval", eff_minutes)
                # Блокировка сама выставила более поздний next_check_at — не затираем его
                if not summary["blocked"]:
                    self.db.set("next_check_at", int(finished + interval + jitter))
                self.db.incr("checks_total")
            if summary["checked"]:
                logger.info(
                    f"Проверка завершена: поисков {summary['checked']}, новых {summary['new']}, "
                    f"ошибок {summary['errors']}, блокировок {summary['blocked']} "
                    f"({finished - started:.1f} с)"
                )
            return summary

    def check_search(self, search: Dict, notify_errors: bool = False, _in_cycle: bool = False):
        """
        Проверяет один поиск. Возвращает:
          int — сколько новых объявлений отправлено/найдено,
          BLOCKED — Avito ограничил доступ,
          строку-ошибку — другая ошибка.
        """
        sid = search["id"]
        self.heartbeat = time.time()
        blocked_until = self.db.get("blocked_until") or 0
        if blocked_until > time.time():
            if notify_errors:
                self.notifier.send_text(
                    search["user_id"],
                    "⏳ Avito временно ограничил доступ, проверки на паузе до "
                    f"{fmt_dt(int(blocked_until))}. Поиск сохранён и будет проверен позже.",
                )
            return BLOCKED

        result = self.fetcher.fetch(search["url"])
        if result.status == BLOCKED:
            self.db.mark_check(sid, False, result.error)
            self._on_blocked(result.error, result.block_reason)
            return BLOCKED
        if result.status not in (OK, EMPTY):
            self.db.mark_check(sid, False, result.error)
            self.db.incr("checks_failed")
            logger.warning(f"Поиск #{sid}: {result.error}")
            if notify_errors:
                self.notifier.send_text(
                    search["user_id"],
                    f"⚠️ Не получилось проверить поиск «{esc(search['title'])}»: {esc(result.error)}.\n"
                    "Я попробую снова при следующей проверке.",
                )
            return result.error or "error"

        self._on_success()
        processor = AvitoProcessor(search["url"], stop_words=settings.stop_words)
        listings = processor.process_html(result.html)
        self.db.mark_check(sid, True)
        self.heartbeat = time.time()
        logger.debug(f"Поиск #{sid}: на странице {len(listings)} объявлений ({result.method})")

        if not search.get("baseline_done"):
            saved = self.db.save_new_items(sid, listings, baseline=True)
            self.notifier.send_text(
                search["user_id"],
                f"✅ Поиск «{esc(search['title'])}» подключён.\n\n"
                f"Сейчас на странице {len(listings)} объявлений — я их запомнил "
                f"(в базу записано {len(saved)}). Теперь буду присылать только <b>новые</b> объявления.",
                url_button=("🔗 Открыть поиск на Avito", search["url"]),
            )
            return 0

        new_rows = self.db.save_new_items(sid, listings)

        # Если на первой странице ВСЕ объявления новые (например, после долгой паузы из‑за
        # блокировки), смотрим следующие страницы, чтобы не потерять объявления.
        page = 1
        page_new = list(new_rows)
        page_ids = {l.item_id for l in listings if l.item_id}
        while page < settings.max_pages and page_ids and len(page_new) == len(page_ids):
            page += 1
            self._sleep(random.uniform(2.0, 5.0))
            extra = self.fetcher.fetch(page_url(search["url"], page))
            if extra.status != OK:
                break
            page_listings = processor.process_html(extra.html)
            page_ids = {l.item_id for l in page_listings if l.item_id}
            page_new = self.db.save_new_items(sid, page_listings)
            logger.info(f"Поиск #{sid}: страница {page}, новых {len(page_new)}")
            new_rows += page_new

        # Объявления, опубликованные задолго до прошлой успешной проверки, — это не новые
        # публикации (реклама, «объявления из других городов» и т.п.). Их не присылаем.
        if settings.skip_old_listings and new_rows:
            reference = search.get("last_success_at") or search.get("created_at") or time.time()
            cutoff = reference - settings.old_listing_margin_hours * 3600
            old_ids = [r["id"] for r in new_rows if (estimate_ts(r) or cutoff) < cutoff]
            if old_ids:
                self.db.mark_skipped(old_ids, status=OLD)
                logger.info(f"Поиск #{sid}: пропущено старых объявлений: {len(old_ids)}")

        pending = sort_freshest_first(self.db.pending_items(sid))
        if not pending:
            return 0

        limit = max(1, settings.max_notifications_per_check)
        to_send, extra = pending[:limit], pending[limit:]
        sent = 0
        for item in to_send:
            try:
                message_id = self.notifier.send_listing(search["user_id"], item, search)
                self.db.mark_sent(item["id"], sid, search["user_id"], message_id)
                sent += 1
            except Exception as e:
                logger.warning(f"Не удалось отправить уведомление (объявление {item['item_id']}): {e}")
                self.db.mark_send_failed(item["id"])
            self._sleep(0.4)  # не превышаем лимиты Telegram
        if extra:
            self.db.mark_skipped([i["id"] for i in extra])
            self.notifier.send_text(
                search["user_id"],
                f"➕ По поиску «{esc(search['title'])}» есть ещё {len(extra)} новых объявлений. "
                "Чтобы не засыпать вас сообщениями, я показал самые свежие — остальные смотрите на Avito.",
                url_button=("🔗 Открыть поиск", search["url"]),
            )
        if sent:
            logger.info(f"Поиск #{sid}: отправлено {sent} новых объявлений")
        return len(pending)

    # ------------------------------------------------------ block/backoff
    def _on_blocked(self, error: Optional[str], reason: Optional[str] = None) -> None:
        level = int(self.db.get("block_level", 0) or 0) + 1
        pause = min(settings.block_pause_minutes * (2 ** (level - 1)), settings.block_pause_max_minutes)
        until = int(time.time() + pause * 60)
        self.db.set("block_level", level)
        self.db.set("blocked_until", until)
        self.db.set("next_check_at", until)
        self.db.set("last_block_reason", reason)
        self.db.set("last_block_at", int(time.time()))
        self.db.incr("blocks_total")
        logger.warning(f"{error or 'Avito ограничил доступ'}. Пауза {pause} мин.")
        if level == 1 or level % 4 == 0:
            hint = {
                REASON_RATE_LIMIT: ("Avito ограничил частоту запросов. Помогает больший интервал "
                                    "проверки (/interval) и меньше поисков."),
            }.get(reason, ("Если блокировка не проходит часами — сервер находится вне России или его IP "
                           "заблокирован Avito, см. раздел «Если Avito блокирует» в инструкции."))
            text = (
                "⛔ <b>Avito временно ограничил доступ</b> к поиску с сервера бота.\n\n"
                f"Причина: {esc(error or 'ограничение доступа')}.\n"
                f"Я делаю паузу на {pause} мин. и затем попробую снова (до {fmt_dt(until)}).\n"
                "Ничего делать не нужно — ваши поиски сохранены.\n\n" + hint
            )
            for admin in self._recipients():
                try:
                    self.notifier.send_text(admin, text)
                except Exception as e:
                    logger.warning(f"Не удалось уведомить о блокировке: {e}")

    def _on_success(self) -> None:
        level = int(self.db.get("block_level", 0) or 0)
        if level:
            self.db.set("block_level", 0)
            self.db.set("blocked_until", 0)
            for admin in self._recipients():
                try:
                    self.notifier.send_text(admin, "✅ Доступ к Avito восстановлен, мониторинг продолжается.")
                except Exception:
                    pass

    def _recipients(self) -> List[int]:
        if self.admin_ids:
            return list(self.admin_ids)
        return sorted({s["user_id"] for s in self.db.list_searches()})

    # ------------------------------------------------------------- status
    def status(self, user_id: Optional[int] = None) -> Dict:
        st = self.db.stats(user_id)
        st.update({
            "paused": self.paused,
            "interval": self.interval_minutes,
            "effective_interval": self.db.get("effective_interval", self.interval_minutes),
            "adaptive": settings.adaptive_interval,
            "idle_streak": int(self.db.get("idle_streak", 0) or 0),
            "last_block_reason": self.db.get("last_block_reason"),
            "last_check_at": self.db.get("last_check_at"),
            "next_check_at": None if self.paused else self.db.get("next_check_at"),
            "blocked_until": self.db.get("blocked_until") or 0,
            "checks_total": self.db.get("checks_total", 0),
            "checks_failed": self.db.get("checks_failed", 0),
            "blocks_total": self.db.get("blocks_total", 0),
            "last_cycle_seconds": self.db.get("last_cycle_seconds"),
            "is_checking": self.is_checking,
            "alive": self.is_alive(),
            "fetch_mode": self.fetcher.mode,
        })
        return st
