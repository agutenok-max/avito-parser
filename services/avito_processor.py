"""
Извлечение объявлений из HTML страницы поиска Avito.

Данные берутся из двух источников:
1. HTML-карточки (div[data-marker="item"]) — основной источник.
2. Встроенный JSON страницы (window.__initialData__) — если он есть,
   из него берутся точные отметки времени (sortTimeStamp) и недостающие поля.
"""
import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin

from bs4 import BeautifulSoup

from config.settings import logger
from database.models import Listing

# Старый список стоп-слов из учебного проекта (используется только CLI main.py)
LEGACY_STOP_WORDS = ['скупка', 'выкуп', 'обмен', 'trade-in', 'трейдин', 'ремонт', 'продажа']

_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

_ITEM_ID_RE = re.compile(r"_(\d{6,})(?:$|[/?#])")


def item_id_from_url(url: str) -> Optional[str]:
    """ID объявления Avito — число в конце адреса: .../nazvanie_1234567890"""
    if not url:
        return None
    m = _ITEM_ID_RE.search(url)
    return m.group(1) if m else None


def parse_relative_date(text: str, now: datetime) -> Tuple[Optional[datetime], bool]:
    """
    Переводит дату с сайта («5 минут назад», «Вчера 12:30», «2 октября 14:00»)
    в datetime. Возвращает (дата, точная_ли). Если разобрать нельзя — (None, False).
    `now` — текущее время в часовом поясе Avito (Москва), без tzinfo.
    """
    if not text:
        return None, False
    t = text.strip().lower().replace("\xa0", " ")

    if t in ("только что", "сейчас"):
        return now, True

    m = re.match(r"^(\d+|несколько)\s+(секунд\w*|минут\w*|час\w*|д(?:ень|ня|ней)|недел\w*|месяц\w*)\s+назад", t)
    if m:
        num = 1 if m.group(1) == "несколько" else int(m.group(1))
        unit = m.group(2)
        if unit.startswith("секунд"):
            return now - timedelta(seconds=num), True
        if unit.startswith("минут"):
            return now - timedelta(minutes=num), True
        if unit.startswith("час"):
            return now - timedelta(hours=num), False
        if unit.startswith("д"):
            return now - timedelta(days=num), False
        if unit.startswith("недел"):
            return now - timedelta(weeks=num), False
        return now - timedelta(days=30 * num), False

    m = re.match(r"^(сегодня|вчера)(?:\s*,?\s*в?\s*(\d{1,2}):(\d{2}))?", t)
    if m:
        base = now if m.group(1) == "сегодня" else now - timedelta(days=1)
        if m.group(2):
            return base.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0), True
        return base.replace(hour=0, minute=0, second=0, microsecond=0), False

    m = re.match(r"^(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?(?:\s*,?\s*в?\s*(\d{1,2}):(\d{2}))?", t)
    if m and m.group(2) in _MONTHS:
        year = int(m.group(3)) if m.group(3) else now.year
        try:
            dt = datetime(year, _MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None, False
        if not m.group(3) and dt > now + timedelta(days=1):
            dt = dt.replace(year=year - 1)  # «28 декабря» в январе — прошлый год
        if m.group(4):
            return dt.replace(hour=int(m.group(4)), minute=int(m.group(5))), True
        return dt, False

    return None, False


class AvitoProcessor:
    """Извлекает структурированные данные из HTML-кода страницы Avito."""

    def __init__(self, base_url: str, stop_words: Optional[List[str]] = None):
        self.base_url = base_url or "https://www.avito.ru/"
        # Стоп-слова для фильтрации по заголовку. По умолчанию фильтр выключен.
        self.stop_words = [w.lower() for w in (stop_words or [])]

    # ------------------------------------------------------------------ API
    def process_html(self, html: str) -> List[Listing]:
        """
        Находит все объявления на странице и извлекает из них данные.
        Порядок — как на странице.
        """
        if not html:
            return []
        soup = BeautifulSoup(html, 'lxml')
        json_items = self._parse_initial_data(soup)

        listings: List[Listing] = []
        seen_ids = set()

        container = soup.find("div", {"data-marker": "catalog-serp"})
        if container is None:
            logger.warning("Основной контейнер с объявлениями ('catalog-serp') не найден.")
        items = container.find_all("div", {"data-marker": "item"}) if container else []
        logger.info(f"Найдено {len(items)} карточек объявлений на странице.")

        for item_soup in items:
            try:
                if item_soup.find("div", {"data-marker": "promo-item"}):
                    continue
                listing = self._parse_item(item_soup)
                if not listing or not listing.url or not listing.title:
                    continue
                if listing.item_id and listing.item_id in seen_ids:
                    continue
                if listing.item_id:
                    seen_ids.add(listing.item_id)
                    self._merge_json(listing, json_items.get(listing.item_id))
                listings.append(listing)
            except Exception as e:
                logger.error(f"Ошибка при обработке карточки объявления: {e}")

        # Если HTML-карточек нет, но есть данные во встроенном JSON — используем их
        if not listings and json_items:
            logger.info(f"Карточки не найдены, используем встроенные данные: {len(json_items)} шт.")
            for item_id, data in json_items.items():
                listing = self._listing_from_json(item_id, data)
                if listing:
                    listings.append(listing)

        return [l for l in listings if not self._is_stopped(l)]

    # ------------------------------------------------------------ helpers
    def _is_stopped(self, listing: Listing) -> bool:
        if not self.stop_words:
            return False
        title_lower = (listing.title or "").lower()
        if any(word in title_lower for word in self.stop_words):
            logger.debug(f"Отфильтровано объявление по стоп-слову: '{listing.title}'")
            return True
        return False

    def _get_text(self, soup: BeautifulSoup, tag: str, attrs: dict) -> Optional[str]:
        """Безопасно извлекает текст из тега."""
        element = soup.find(tag, attrs)
        if not element:
            return None
        if tag == "meta":
            return (element.get('content') or '').strip() or None
        return element.get_text(" ", strip=True) or None

    def _parse_images(self, item_soup: BeautifulSoup) -> List[str]:
        """Извлекает ссылки на изображения из карусели, выбирая самые большие."""
        images: List[str] = []
        gallery = (item_soup.find('div', {'data-marker': 'item-photo'})
                   or item_soup.find(attrs={'data-marker': re.compile(r'^item-(photo|image)')})
                   or item_soup)
        for img in gallery.find_all("img")[:3]:
            url = None
            srcset = img.get('srcset')
            if srcset:
                parts = [p.strip() for p in srcset.split(',') if p.strip()]
                if parts:
                    url = parts[-1].split(' ')[0]
            if not url:
                url = img.get('src')
            if url and url.startswith("http") and url not in images:
                images.append(url)
        return images

    def _parse_item(self, item_soup: BeautifulSoup) -> Optional[Listing]:
        """Извлекает данные из одной карточки объявления."""
        link_tag = item_soup.find("a", {"data-marker": "item-title"})
        if not link_tag or not link_tag.has_attr('href'):
            return None

        relative_url = link_tag['href']
        absolute_url = urljoin(self.base_url, relative_url).split("?")[0]
        title = link_tag.get('title') or link_tag.get_text(strip=True) or None
        if title:
            title = re.sub(r"\s+", " ", title).strip()

        price = self._get_text(item_soup, "meta", {"itemprop": "price"})
        if price and price.isdigit():
            price = f"{int(price):,}".replace(",", " ") + " ₽"
        if not price:
            price_tag = item_soup.find(attrs={"data-marker": "item-price"})
            price = price_tag.get_text(" ", strip=True) if price_tag else None

        description = self._get_text(item_soup, "meta", {"itemprop": "description"})
        if not description:
            desc_tag = item_soup.find(attrs={"data-marker": "item-description"}) \
                or item_soup.select_one('div[class*="item-description"], p[class*="description"]')
            description = desc_tag.get_text(" ", strip=True) if desc_tag else None

        address_div = item_soup.select_one('div[class*="geo-root-"], [data-marker="item-address"]')
        address = address_div.get_text(" ", strip=True) if address_div else None
        if not address:
            url_parts = relative_url.lstrip('/').split('/')
            if url_parts and url_parts[0] and url_parts[0] not in ('all', 'www'):
                address = url_parts[0].replace('_', ' ').title()

        date_tag = item_soup.find(attrs={"data-marker": "item-date"})
        published_text = date_tag.get_text(" ", strip=True) if date_tag else None

        item_id = item_soup.get("data-item-id") or item_id_from_url(absolute_url)

        return Listing(
            url=absolute_url,
            title=title,
            price=price,
            address=address,
            description=description,
            images=self._parse_images(item_soup),
            item_id=str(item_id) if item_id else None,
            published_text=published_text,
        )

    # ---------------------------------------------------- встроенный JSON
    def _parse_initial_data(self, soup: BeautifulSoup) -> Dict[str, dict]:
        """Ищет в странице JSON с объявлениями и возвращает {id: данные}."""
        result: Dict[str, dict] = {}
        for script in soup.find_all("script"):
            text = script.string or script.get_text() or ""
            if "__initialData__" not in text and "sortTimeStamp" not in text:
                continue
            for payload in self._extract_json_payloads(text):
                self._collect_items(payload, result, depth=0)
        return result

    @staticmethod
    def _extract_json_payloads(text: str):
        m = re.search(r'__initialData__\s*=\s*"(.*?)"\s*;?\s*(?:window\.|$|</)', text, re.S)
        candidates = []
        if m:
            candidates.append(unquote(m.group(1)))
        else:
            stripped = text.strip()
            if stripped.startswith("{"):
                candidates.append(stripped)
        for raw in candidates:
            try:
                yield json.loads(raw)
            except Exception:
                continue

    def _collect_items(self, node, result: Dict[str, dict], depth: int):
        if depth > 25:
            return
        if isinstance(node, dict):
            if "id" in node and ("urlPath" in node or "sortTimeStamp" in node) and "title" in node:
                result.setdefault(str(node["id"]), node)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    self._collect_items(value, result, depth + 1)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    self._collect_items(value, result, depth + 1)

    @staticmethod
    def _json_ts(data: dict) -> Optional[int]:
        ts = data.get("sortTimeStamp")
        if isinstance(ts, (int, float)) and ts > 0:
            return int(ts / 1000) if ts > 10**11 else int(ts)
        return None

    def _merge_json(self, listing: Listing, data: Optional[dict]):
        if not data:
            return
        listing.published_ts = self._json_ts(data) or listing.published_ts
        if not listing.description and isinstance(data.get("description"), str):
            listing.description = data["description"].strip() or None
        if not listing.images:
            listing.images = self._json_images(data)

    @staticmethod
    def _json_images(data: dict) -> List[str]:
        images = []
        for img in (data.get("images") or [])[:3]:
            if isinstance(img, dict) and img:
                # берём ссылку на самый большой размер
                def size_key(k):
                    m = re.match(r"(\d+)x(\d+)", str(k))
                    return int(m.group(1)) * int(m.group(2)) if m else 0
                best = max(img.keys(), key=size_key)
                if isinstance(img[best], str) and img[best].startswith("http"):
                    images.append(img[best])
        return images

    def _listing_from_json(self, item_id: str, data: dict) -> Optional[Listing]:
        url_path = data.get("urlPath")
        title = data.get("title")
        if not url_path or not title:
            return None
        price = None
        pd = data.get("priceDetailed")
        if isinstance(pd, dict):
            price = pd.get("fullString") or pd.get("string")
            if not price and pd.get("value"):
                price = f"{int(pd['value']):,}".replace(",", " ") + " ₽"
        address = None
        loc = data.get("location")
        if isinstance(loc, dict):
            address = loc.get("name")
        geo = data.get("geo")
        if isinstance(geo, dict) and geo.get("formattedAddress"):
            address = geo["formattedAddress"]
        return Listing(
            url=urljoin("https://www.avito.ru", url_path).split("?")[0],
            title=str(title).strip(),
            price=price,
            address=address,
            description=(data.get("description") or None),
            images=self._json_images(data),
            item_id=str(item_id),
            published_ts=self._json_ts(data),
        )
