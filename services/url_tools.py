"""Проверка и нормализация ссылок на поиск Avito."""
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

# Значение параметра сортировки Avito «По дате»
SORT_BY_DATE = "104"

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


@dataclass
class UrlCheck:
    ok: bool
    url: Optional[str] = None
    error: Optional[str] = None


def extract_url(text: str) -> Optional[str]:
    """Достаёт первую ссылку из текста (например, из «Поделиться» в приложении Avito)."""
    if not text:
        return None
    m = _URL_RE.search(text)
    if m:
        return m.group(0).rstrip(").,;!")
    t = text.strip()
    if t.lower().startswith(("avito.ru", "www.avito.ru", "m.avito.ru")):
        return "https://" + t.split()[0]
    return None


def normalize_search_url(text: str) -> UrlCheck:
    """
    Проверяет, что это ссылка на страницу ПОИСКА Avito, и приводит её к виду
    https://www.avito.ru/...?...&s=104 (сортировка по дате — сначала новые).
    """
    raw = extract_url(text)
    if not raw:
        return UrlCheck(False, error="Я не нашёл ссылку в сообщении. Скопируйте адрес страницы поиска Avito и пришлите его.")

    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if not (host == "avito.ru" or host.endswith(".avito.ru")):
        return UrlCheck(False, error="Это не ссылка Avito. Нужна ссылка вида https://www.avito.ru/...")

    if host in ("avito.ru", "m.avito.ru"):
        host = "www.avito.ru"

    path = parts.path or "/"
    if re.search(r"_\d{6,}/?$", path):
        return UrlCheck(
            False,
            error=("Это ссылка на одно объявление, а нужна ссылка на страницу поиска "
                   "(список объявлений). Откройте поиск на Avito, настройте фильтры и "
                   "скопируйте ссылку на результаты."),
        )
    if path in ("", "/") and not parts.query:
        return UrlCheck(False, error="Это главная страница Avito. Откройте нужный поиск с фильтрами и пришлите ссылку на него.")

    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in ("s", "p", "utm_source", "utm_medium", "utm_campaign", "utm_content")]
    query.append(("s", SORT_BY_DATE))
    url = urlunsplit(("https", host, path, urlencode(query, doseq=True), ""))
    return UrlCheck(True, url=url)


def search_title_from_url(url: str) -> str:
    """Короткое понятное название поиска по ссылке."""
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query)).get("q")
    segments = [s for s in parts.path.split("/") if s]
    city = segments[0] if segments else ""
    if q:
        name = unquote_plus(q)
    else:
        cat = segments[-1] if len(segments) > 1 else (segments[0] if segments else "поиск")
        cat = re.sub(r"-[A-Za-z0-9~]{10,}$", "", cat)  # хвост с зашифрованными фильтрами
        name = cat.replace("_", " ").replace("-", " ")
    if city and city not in ("all",) and city not in name:
        name = f"{name} ({city.replace('_', ' ')})"
    name = name.strip() or "Поиск Avito"
    return name[:60]


def page_url(url: str, page: int) -> str:
    """Ссылка на страницу N результатов поиска (p=N)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "p"]
    if page > 1:
        query.append(("p", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))
