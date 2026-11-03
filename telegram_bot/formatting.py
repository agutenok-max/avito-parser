"""Тексты сообщений бота."""
import html
import time
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

from config.settings import settings

try:
    TZ = ZoneInfo(settings.timezone)
except Exception:  # pragma: no cover
    TZ = ZoneInfo("Europe/Moscow")

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096
FRESH_MINUTES = 60  # «опубликовано недавно» — не старше часа


def esc(value) -> str:
    return html.escape(str(value), quote=False) if value is not None else ""


def fmt_dt(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


def fmt_ago(ts: Optional[int], now: Optional[float] = None) -> str:
    if not ts:
        return "никогда"
    diff = int((now or time.time()) - ts)
    if diff < 0:
        diff = -diff
        if diff < 60:
            return "меньше чем через минуту"
        if diff < 3600:
            return f"через {diff // 60} мин."
        return f"через {diff // 3600} ч. {diff % 3600 // 60} мин."
    if diff < 60:
        return "только что"
    if diff < 3600:
        return f"{diff // 60} мин. назад"
    if diff < 86400:
        return f"{diff // 3600} ч. назад"
    return f"{diff // 86400} дн. назад"


def short(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def publication_line(item: Dict, now: Optional[float] = None) -> str:
    """Строка с датой публикации. Если дата неизвестна — так и пишем."""
    now = now or time.time()
    ts = item.get("published_ts")
    raw = item.get("published_text")
    if ts:
        line = f"{fmt_dt(ts)} ({fmt_ago(ts, now)})"
    elif raw:
        line = f"{esc(raw)} (по данным Avito)"
    else:
        return "не указана на Avito"
    return line


def is_fresh(item: Dict, now: Optional[float] = None) -> bool:
    now = now or time.time()
    ts = item.get("published_ts")
    if ts:
        return 0 <= now - ts <= FRESH_MINUTES * 60
    raw = (item.get("published_text") or "").lower()
    return any(w in raw for w in ("только что", "секунд", "минут", "1 час назад"))


def notification_text(item: Dict, search_title: str, with_photo: bool, now: Optional[float] = None) -> str:
    now = now or time.time()
    header = "🔔 <b>НОВОЕ ОБЪЯВЛЕНИЕ AVITO</b>"
    if is_fresh(item, now):
        header += "\n🔥 <i>Опубликовано только что</i>"

    def build(desc_limit: int) -> str:
        lines = [
            header,
            "",
            f"📌 <b>Название:</b> {esc(short(item.get('title'), 200))}",
            f"💰 <b>Цена:</b> {esc(item.get('price') or 'не указана')}",
            f"📍 <b>Местоположение:</b> {esc(short(item.get('address'), 150) or 'не указано')}",
            f"🕒 <b>Дата публикации:</b> {publication_line(item, now)}",
        ]
        desc = short(item.get("description"), desc_limit)
        if desc:
            lines.append(f"📝 <b>Описание:</b> {esc(desc)}")
        lines.append(f"🔗 <b>Ссылка:</b> {esc(item.get('url'))}")
        if item.get("images") and not with_photo:
            lines.append(f"📷 <b>Фото:</b> {len(item['images'])} шт. (откройте объявление)")
        lines.append(f"\n🔎 <i>Поиск: {esc(short(search_title, 60))}</i>")
        return "\n".join(lines)

    limit = CAPTION_LIMIT if with_photo else MESSAGE_LIMIT
    for desc_limit in (300, 150, 60, 0):
        text = build(desc_limit)
        if len(text) <= limit:
            return text
    return text[:limit]
