"""
Клавиатуры для Telegram-бота
"""
from typing import Dict, List

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from telegram_bot.formatting import short

INTERVAL_CHOICES = [3, 5, 10, 15, 30, 60]


def main_menu(paused: bool = False) -> InlineKeyboardMarkup:
    """Главное меню бота"""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("➕ Добавить поиск", callback_data="add"),
        InlineKeyboardButton("📋 Мои поиски", callback_data="list"),
    )
    markup.add(
        InlineKeyboardButton("🗑 Удалить поиск", callback_data="remove"),
        InlineKeyboardButton("▶️ Продолжить" if paused else "⏸ Пауза",
                             callback_data="resume" if paused else "pause"),
    )
    markup.add(
        InlineKeyboardButton("📊 Статистика", callback_data="status"),
        InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
    )
    markup.add(
        InlineKeyboardButton("🔄 Проверить сейчас", callback_data="check_all"),
        InlineKeyboardButton("📖 Последние находки", callback_data="journal:0"),
    )
    return markup


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup().add(InlineKeyboardButton("🏠 В меню", callback_data="menu"))


def cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup().add(InlineKeyboardButton("✖️ Отмена", callback_data="cancel"))


def searches_list(searches: List[Dict]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=3)
    for s in searches:
        icon = "🟢" if s["is_active"] else "⏸"
        markup.row(InlineKeyboardButton(f"{icon} {short(s['title'], 40)}", url=s["url"]))
        markup.row(
            InlineKeyboardButton("⏸ Пауза" if s["is_active"] else "▶️ Вкл.", callback_data=f"toggle:{s['id']}"),
            InlineKeyboardButton("🔄 Проверить", callback_data=f"check:{s['id']}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"del:{s['id']}"),
        )
    markup.row(
        InlineKeyboardButton("➕ Добавить поиск", callback_data="add"),
        InlineKeyboardButton("🏠 В меню", callback_data="menu"),
    )
    return markup


def remove_list(searches: List[Dict]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for s in searches:
        markup.add(InlineKeyboardButton(f"🗑 {short(s['title'], 45)}", callback_data=f"del:{s['id']}"))
    markup.add(InlineKeyboardButton("🏠 В меню", callback_data="menu"))
    return markup


def confirm_delete(search_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"delok:{search_id}"),
        InlineKeyboardButton("✖️ Нет", callback_data="list"),
    )
    return markup


def settings_menu(interval: int, photos: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(f"⏱ Интервал проверки: {interval} мин.", callback_data="interval"),
        InlineKeyboardButton(f"📷 Фото в уведомлениях: {'вкл.' if photos else 'выкл.'}",
                             callback_data="photos"),
        InlineKeyboardButton("🏠 В меню", callback_data="menu"),
    )
    return markup


def interval_menu(current: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(*[
        InlineKeyboardButton(f"{'✅ ' if m == current else ''}{m} мин.", callback_data=f"setint:{m}")
        for m in INTERVAL_CHOICES
    ])
    markup.add(InlineKeyboardButton("🔙 Назад", callback_data="settings"))
    return markup


def listing_buttons(url: str, search_url: str = None) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("🔗 Открыть объявление", url=url))
    return markup


def journal_navigation(offset: int, page_size: int, has_more: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ Новее", callback_data=f"journal:{max(0, offset - page_size)}"))
    if has_more:
        nav.append(InlineKeyboardButton("Старее ➡️", callback_data=f"journal:{offset + page_size}"))
    if nav:
        markup.add(*nav)
    markup.add(InlineKeyboardButton("🏠 В меню", callback_data="menu"))
    return markup
