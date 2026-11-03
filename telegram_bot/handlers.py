"""
Обработчики команд и кнопок Telegram-бота.
"""
import time
from typing import List, Optional

from loguru import logger
from telebot.apihelper import ApiTelegramException
from telebot.types import BotCommand

from config.settings import settings
from database.monitor_db import MonitorDB
from services.url_tools import normalize_search_url, search_title_from_url
from telegram_bot import keyboards as kb
from telegram_bot.formatting import esc, fmt_ago, fmt_dt, publication_line, short
from telegram_bot.monitor import Monitor

JOURNAL_PAGE = 5

COMMANDS = [
    ("start", "Главное меню"),
    ("add", "Добавить поиск Avito"),
    ("list", "Мои поиски"),
    ("remove", "Удалить поиск"),
    ("pause", "Приостановить мониторинг"),
    ("resume", "Возобновить мониторинг"),
    ("status", "Состояние и статистика"),
    ("interval", "Интервал проверки"),
    ("help", "Инструкция"),
]

HELP_TEXT = (
    "📖 <b>Как пользоваться ботом</b>\n\n"
    "<b>1. Добавьте поиск.</b> Откройте Avito (сайт или приложение), найдите нужные товары, "
    "настройте фильтры (город, цена и т.д.). Нажмите «Поделиться» → «Скопировать ссылку» "
    "(или скопируйте адрес страницы в Safari). Затем нажмите «➕ Добавить поиск» и пришлите ссылку.\n\n"
    "<b>2. Ждите уведомлений.</b> Бот запомнит объявления, которые уже есть, и будет присылать "
    "только новые. Проверка — автоматически каждые несколько минут, круглосуточно, даже если "
    "телефон выключен.\n\n"
    "<b>Команды:</b>\n"
    "/add — добавить ссылку на поиск\n"
    "/list — мои поиски (пауза, проверка, удаление)\n"
    "/remove — удалить поиск\n"
    "/pause — приостановить все проверки\n"
    "/resume — возобновить проверки\n"
    "/status — статистика и время проверок\n"
    "/interval — изменить интервал (например: <code>/interval 5</code>)\n"
    "/help — эта инструкция\n\n"
    "💡 Ссылку можно прислать и просто сообщением — бот поймёт.\n"
    "💡 Бот сам включает сортировку «по дате», чтобы видеть свежие объявления первыми.\n"
    "⚠️ Если Avito временно ограничит доступ, бот сам сделает паузу и сообщит об этом."
)


def register_handlers(bot, db: MonitorDB, monitor: Monitor, admin_ids: Optional[List[int]] = None):
    """Регистрирует все обработчики"""
    admins = list(settings.admin_ids if admin_ids is None else admin_ids)
    awaiting_url = set()

    # ------------------------------------------------------------ access
    def allowed(user_id: int) -> bool:
        return user_id in admins

    def deny(chat_id: int, user_id: int) -> None:
        if not admins:
            bot.send_message(
                chat_id,
                "🔐 <b>Бот ещё не привязан к владельцу.</b>\n\n"
                f"Ваш Telegram ID: <code>{user_id}</code>\n\n"
                "Чтобы бот слушался только вас, добавьте на хостинге переменную\n"
                f"<code>TELEGRAM_ADMIN_ID</code> = <code>{user_id}</code>\n"
                "и перезапустите сервис (подробно — в инструкции DEPLOY.md, шаг «Telegram ID»).",
            )
            logger.info(f"Запрос /start без настроенного TELEGRAM_ADMIN_ID, user_id={user_id}")
        else:
            bot.send_message(chat_id, "⛔ Это личный бот. Доступ к управлению закрыт.")
            logger.warning(f"Попытка доступа от постороннего пользователя id={user_id}")

    def guard(message) -> bool:
        if allowed(message.from_user.id):
            return True
        deny(message.chat.id, message.from_user.id)
        return False

    # ----------------------------------------------------------- helpers
    def menu_text() -> str:
        st = monitor.status()
        state = "⏸ на паузе" if st["paused"] else "🟢 работает"
        return (
            "🏠 <b>Главное меню</b>\n\n"
            f"Мониторинг: {state}\n"
            f"Активных поисков: {st['searches_active']} из {st['searches_total']}\n"
            f"Интервал проверки: {st['interval']} мин.\n\n"
            "Выберите действие:"
        )

    def show(chat_id: int, text: str, markup=None, message_id: Optional[int] = None) -> None:
        """Редактирует сообщение (если нажата кнопка) или отправляет новое."""
        if message_id:
            try:
                bot.edit_message_text(text, chat_id, message_id, reply_markup=markup,
                                      disable_web_page_preview=True)
                return
            except ApiTelegramException as e:
                if "message is not modified" in str(e):
                    return
            except Exception:
                pass
        bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)

    def status_text(user_id: int) -> str:
        st = monitor.status(user_id)
        now = time.time()
        lines = ["📊 <b>Статистика и состояние</b>\n"]
        if st["paused"]:
            lines.append("Мониторинг: ⏸ <b>на паузе</b> (нажмите «Продолжить»)")
        elif st["blocked_until"] and st["blocked_until"] > now:
            lines.append(f"Мониторинг: ⛔ пауза из‑за ограничения Avito до {fmt_dt(st['blocked_until'])}")
        elif st["is_checking"]:
            lines.append("Мониторинг: 🔄 идёт проверка…")
        else:
            lines.append("Мониторинг: 🟢 работает")
        lines += [
            f"🔎 Активных поисков: <b>{st['searches_active']}</b> (всего {st['searches_total']})",
            f"📦 Новых объявлений найдено: <b>{st['items_new']}</b> (за сутки: {st['items_last_day']})",
            f"🗂 Всего объявлений в базе: {st['items_total']}",
            f"📨 Уведомлений отправлено: <b>{st['notifications_sent']}</b>",
            "",
            f"⏱ Интервал проверки: {st['interval']} мин.",
            f"🕒 Последняя проверка: {fmt_dt(st['last_check_at'])} ({fmt_ago(st['last_check_at'], now)})"
            if st["last_check_at"] else "🕒 Последняя проверка: ещё не было",
        ]
        if st["paused"]:
            lines.append("⏭ Следующая проверка: после снятия паузы")
        elif st["next_check_at"]:
            nxt = max(st["next_check_at"], st["blocked_until"] or 0)
            lines.append(f"⏭ Следующая проверка: {fmt_dt(nxt)} ({fmt_ago(nxt, now)})")
        lines.append(f"🔁 Всего циклов проверки: {st['checks_total']}, ограничений Avito: {st['blocks_total']}")
        errors = [s for s in db.list_searches(user_id) if s.get("last_error")]
        if errors:
            lines.append("\n⚠️ <b>Последние ошибки:</b>")
            for s in errors[:5]:
                lines.append(f"• {esc(short(s['title'], 30))}: {esc(short(s['last_error'], 120))}")
        return "\n".join(lines)

    def list_text(user_id: int) -> str:
        searches = db.list_searches(user_id)
        if not searches:
            return "📋 <b>Мои поиски</b>\n\nПока нет ни одного поиска. Нажмите «➕ Добавить поиск»."
        lines = [f"📋 <b>Мои поиски</b> ({len(searches)})\n"]
        for i, s in enumerate(searches, 1):
            state = "🟢 активен" if s["is_active"] else "⏸ на паузе"
            checked = fmt_ago(s["last_check_at"]) if s["last_check_at"] else "ещё не проверялся"
            lines.append(
                f"<b>{i}. {esc(short(s['title'], 50))}</b>\n"
                f"   {state} · проверен: {checked}\n"
                f"   найдено новых: {s['items_found']}, "
                f"отправлено: {s['notifications_sent']}"
            )
            if s.get("last_error"):
                lines.append(f"   ⚠️ {esc(short(s['last_error'], 80))}")
        if monitor.paused:
            lines.append("\n⏸ Мониторинг сейчас на паузе для всех поисков.")
        return "\n".join(lines)

    def start_add(chat_id: int, user_id: int, message_id: Optional[int] = None) -> None:
        if db.count_searches(user_id) >= settings.max_searches:
            show(chat_id, f"⚠️ Достигнут лимит: {settings.max_searches} поисков. Удалите ненужный поиск.",
                 kb.back_menu(), message_id)
            return
        awaiting_url.add(user_id)
        show(
            chat_id,
            "➕ <b>Добавление поиска</b>\n\n"
            "Пришлите ссылку на страницу поиска Avito.\n\n"
            "Как её получить на iPhone:\n"
            "1. Откройте Avito, найдите товары и настройте фильтры.\n"
            "2. Нажмите «Поделиться» (квадрат со стрелкой) → «Скопировать».\n"
            "3. Вставьте ссылку сюда и отправьте.",
            kb.cancel_menu(), message_id,
        )

    def add_search(message, text: str) -> None:
        user_id = message.from_user.id
        check = normalize_search_url(text)
        if not check.ok:
            bot.reply_to(message, f"❌ {check.error}", reply_markup=kb.cancel_menu())
            return
        if db.count_searches(user_id) >= settings.max_searches:
            awaiting_url.discard(user_id)
            bot.reply_to(message, f"⚠️ Достигнут лимит: {settings.max_searches} поисков.", reply_markup=kb.back_menu())
            return
        title = search_title_from_url(check.url)
        search_id = db.add_search(user_id, check.url, title)
        awaiting_url.discard(user_id)
        if search_id is None:
            bot.reply_to(message, "ℹ️ Этот поиск уже отслеживается.", reply_markup=kb.main_menu(monitor.paused))
            return
        logger.info(f"Добавлен поиск #{search_id}: {title}")
        extra = "\n\n⏸ Мониторинг сейчас на паузе — нажмите «Продолжить», чтобы получать уведомления." \
            if monitor.paused else ""
        bot.reply_to(
            message,
            f"👍 Поиск «<b>{esc(title)}</b>» добавлен.\n\n"
            "Сейчас открою страницу и запомню текущие объявления — через минуту пришлю подтверждение. "
            f"После этого буду присылать только новые.{extra}",
            reply_markup=kb.main_menu(monitor.paused),
            disable_web_page_preview=True,
        )
        monitor.check_now(search_id)

    def set_interval_value(chat_id: int, value: int, message_id: Optional[int] = None) -> None:
        minutes = monitor.set_interval(value)
        note = ""
        if value < settings.min_interval_minutes:
            note = f"\n(минимум — {settings.min_interval_minutes} мин., чтобы не нагружать Avito)"
        show(chat_id, f"✅ Интервал проверки: <b>{minutes} мин.</b>{note}",
             kb.settings_menu(minutes, bool(db.get("send_photos", settings.send_photos))), message_id)

    # ---------------------------------------------------------- commands
    @bot.message_handler(commands=['start'])
    def cmd_start(message):
        user = message.from_user
        if not guard(message):
            return
        db.upsert_user(user.id, user.username, user.first_name)
        awaiting_url.discard(user.id)
        bot.send_message(
            message.chat.id,
            f"👋 <b>Здравствуйте, {esc(user.first_name or 'друг')}!</b>\n\n"
            "Я слежу за новыми объявлениями на Avito и сразу присылаю их вам.\n"
            "Добавьте ссылку на поиск — и я буду проверять её круглосуточно.\n\n"
            f"Ваш Telegram ID: <code>{user.id}</code>\n\n" + menu_text(),
            reply_markup=kb.main_menu(monitor.paused),
        )

    @bot.message_handler(commands=['help'])
    def cmd_help(message):
        if guard(message):
            bot.send_message(message.chat.id, HELP_TEXT, reply_markup=kb.back_menu())

    @bot.message_handler(commands=['add'])
    def cmd_add(message):
        if not guard(message):
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            add_search(message, parts[1])
        else:
            start_add(message.chat.id, message.from_user.id)

    @bot.message_handler(commands=['list'])
    def cmd_list(message):
        if guard(message):
            uid = message.from_user.id
            bot.send_message(message.chat.id, list_text(uid), reply_markup=kb.searches_list(db.list_searches(uid)),
                             disable_web_page_preview=True)

    @bot.message_handler(commands=['remove'])
    def cmd_remove(message):
        if not guard(message):
            return
        searches = db.list_searches(message.from_user.id)
        if not searches:
            bot.send_message(message.chat.id, "Удалять нечего — поисков пока нет.", reply_markup=kb.back_menu())
            return
        bot.send_message(message.chat.id, "🗑 Какой поиск удалить?", reply_markup=kb.remove_list(searches))

    @bot.message_handler(commands=['pause'])
    def cmd_pause(message):
        if guard(message):
            monitor.pause()
            bot.send_message(message.chat.id, "⏸ Мониторинг приостановлен. Поиски сохранены.\n"
                             "Чтобы продолжить — /resume или кнопка «Продолжить».",
                             reply_markup=kb.main_menu(True))

    @bot.message_handler(commands=['resume'])
    def cmd_resume(message):
        if guard(message):
            monitor.resume()
            bot.send_message(message.chat.id, "▶️ Мониторинг возобновлён. Проверка начнётся в течение минуты.",
                             reply_markup=kb.main_menu(False))

    @bot.message_handler(commands=['status', 'stats'])
    def cmd_status(message):
        if guard(message):
            bot.send_message(message.chat.id, status_text(message.from_user.id), reply_markup=kb.back_menu())

    @bot.message_handler(commands=['interval'])
    def cmd_interval(message):
        if not guard(message):
            return
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            value = parts[1].strip().split()[0]
            if not value.isdigit() or int(value) <= 0:
                bot.reply_to(message, "Укажите число минут, например: <code>/interval 5</code>")
                return
            set_interval_value(message.chat.id, int(value))
        else:
            bot.send_message(
                message.chat.id,
                f"⏱ Сейчас проверка каждые <b>{monitor.interval_minutes} мин.</b>\n"
                "Выберите новый интервал или отправьте, например, <code>/interval 7</code>:",
                reply_markup=kb.interval_menu(monitor.interval_minutes),
            )

    @bot.message_handler(func=lambda m: True, content_types=['text'])
    def on_text(message):
        if not guard(message):
            return
        user_id = message.from_user.id
        text = message.text or ""
        if user_id in awaiting_url or "avito.ru" in text.lower():
            add_search(message, text)
            return
        bot.send_message(message.chat.id, "Не понял сообщение 🙂 Выберите действие в меню или пришлите "
                         "ссылку на поиск Avito.", reply_markup=kb.main_menu(monitor.paused))

    # ----------------------------------------------------------- buttons
    @bot.callback_query_handler(func=lambda call: True)
    def handle_callback(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        message_id = call.message.message_id
        data = call.data or ""

        def answer(text: str = None, alert: bool = False):
            try:
                bot.answer_callback_query(call.id, text, show_alert=alert)
            except Exception:
                pass

        if not allowed(user_id):
            answer("⛔ Доступ закрыт", alert=True)
            return

        try:
            if data == "menu":
                awaiting_url.discard(user_id)
                answer()
                show(chat_id, menu_text(), kb.main_menu(monitor.paused), message_id)

            elif data == "add":
                answer()
                start_add(chat_id, user_id, message_id)

            elif data == "cancel":
                awaiting_url.discard(user_id)
                answer("Отменено")
                show(chat_id, menu_text(), kb.main_menu(monitor.paused), message_id)

            elif data == "list":
                answer()
                show(chat_id, list_text(user_id), kb.searches_list(db.list_searches(user_id)), message_id)

            elif data == "remove":
                answer()
                searches = db.list_searches(user_id)
                if not searches:
                    show(chat_id, "Удалять нечего — поисков пока нет.", kb.back_menu(), message_id)
                else:
                    show(chat_id, "🗑 Какой поиск удалить?", kb.remove_list(searches), message_id)

            elif data.startswith("del:"):
                answer()
                s = db.get_search(int(data.split(":")[1]))
                if not s or s["user_id"] != user_id:
                    show(chat_id, "Поиск не найден (возможно, уже удалён).", kb.back_menu(), message_id)
                else:
                    show(chat_id, f"Удалить поиск «<b>{esc(s['title'])}</b>»?\n"
                         "История объявлений по нему тоже будет удалена.",
                         kb.confirm_delete(s["id"]), message_id)

            elif data.startswith("delok:"):
                s = db.get_search(int(data.split(":")[1]))
                if s and s["user_id"] == user_id:
                    db.delete_search(s["id"])
                    answer("Поиск удалён")
                    logger.info(f"Удалён поиск #{s['id']}")
                else:
                    answer("Поиск уже удалён")
                show(chat_id, list_text(user_id), kb.searches_list(db.list_searches(user_id)), message_id)

            elif data.startswith("toggle:"):
                s = db.get_search(int(data.split(":")[1]))
                if s and s["user_id"] == user_id:
                    db.set_search_active(s["id"], not s["is_active"])
                    answer("Поиск приостановлен" if s["is_active"] else "Поиск включён")
                show(chat_id, list_text(user_id), kb.searches_list(db.list_searches(user_id)), message_id)

            elif data.startswith("check:"):
                s = db.get_search(int(data.split(":")[1]))
                if s and s["user_id"] == user_id:
                    monitor.check_now(s["id"])
                    answer("🔄 Проверяю… Новые объявления придут отдельными сообщениями.", alert=True)
                else:
                    answer("Поиск не найден")

            elif data == "check_all":
                if not db.list_searches(user_id, only_active=True):
                    answer("Нет активных поисков. Сначала добавьте поиск.", alert=True)
                else:
                    monitor.check_now()
                    answer("🔄 Запускаю проверку всех поисков. Новые объявления придут отдельными сообщениями.",
                           alert=True)

            elif data == "pause":
                monitor.pause()
                answer("⏸ Мониторинг приостановлен")
                show(chat_id, menu_text(), kb.main_menu(True), message_id)

            elif data == "resume":
                monitor.resume()
                answer("▶️ Мониторинг возобновлён")
                show(chat_id, menu_text(), kb.main_menu(False), message_id)

            elif data == "status":
                answer()
                markup = kb.back_menu()
                show(chat_id, status_text(user_id), markup, message_id)

            elif data == "settings":
                answer()
                show(chat_id, "⚙️ <b>Настройки</b>\n\nНажмите на параметр, чтобы изменить его.",
                     kb.settings_menu(monitor.interval_minutes, bool(db.get("send_photos", settings.send_photos))),
                     message_id)

            elif data == "interval":
                answer()
                show(chat_id, f"⏱ Сейчас проверка каждые <b>{monitor.interval_minutes} мин.</b>\n"
                     "Выберите новый интервал (или отправьте команду, например <code>/interval 7</code>):",
                     kb.interval_menu(monitor.interval_minutes), message_id)

            elif data.startswith("setint:"):
                answer("Интервал изменён")
                set_interval_value(chat_id, int(data.split(":")[1]), message_id)

            elif data == "photos":
                new_value = not bool(db.get("send_photos", settings.send_photos))
                db.set("send_photos", new_value)
                answer("Фото включены" if new_value else "Фото выключены")
                show(chat_id, "⚙️ <b>Настройки</b>\n\nНажмите на параметр, чтобы изменить его.",
                     kb.settings_menu(monitor.interval_minutes, new_value), message_id)

            elif data.startswith("journal:"):
                answer()
                offset = max(0, int(data.split(":")[1]))
                items = db.recent_items(user_id, JOURNAL_PAGE + 1, offset)
                has_more = len(items) > JOURNAL_PAGE
                items = items[:JOURNAL_PAGE]
                if not items:
                    show(chat_id, "📖 Пока нет новых объявлений. Как только появятся — пришлю уведомление.",
                         kb.back_menu(), message_id)
                else:
                    lines = ["📖 <b>Последние найденные объявления</b>\n"]
                    for it in items:
                        lines.append(
                            f"• <a href=\"{esc(it['url'])}\">{esc(short(it['title'], 60))}</a>\n"
                            f"  💰 {esc(it.get('price') or '—')} · 🕒 {publication_line(it)}"
                        )
                    show(chat_id, "\n".join(lines), kb.journal_navigation(offset, JOURNAL_PAGE, has_more), message_id)

            elif data == "noop":
                answer()
            else:
                answer()

        except Exception as e:
            logger.exception(f"Ошибка обработки кнопки {data}: {e}")
            answer("Произошла ошибка, попробуйте ещё раз", alert=True)


def setup_commands(bot) -> None:
    try:
        bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
    except Exception as e:
        logger.warning(f"Не удалось установить список команд: {e}")
