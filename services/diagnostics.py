"""
Временная диагностика доступа к Avito (команда /diagnose).

Ровно по одному GET-запросу к каждому адресу, без повторов. Запросы идут с теми же
заголовками и тем же сетевым маршрутом, что и обычный мониторинг (AvitoFetcher), чтобы
результат отражал реальную ситуацию. Cookies, заголовки ответа и секреты не выводятся.
Основная логика парсера не меняется.
"""
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from config.settings import redact, settings
from services.avito_fetcher import USER_AGENT

AVITO_HOME = "https://www.avito.ru/"
IP_SERVICE = "https://ipinfo.io/json"

# Признаки CAPTCHA / проверки робота / ограничения доступа в HTML
ROBOT_MARKERS = {
    "captcha": r"captcha",
    "geetest": r"geetest",
    "hcaptcha": r"hcaptcha",
    "recaptcha": r"recaptcha",
    "«Доступ ограничен»": r"доступ ограничен",
    "«Проблема с IP»": r"проблема с ip",
    "«проверка безопасности»": r"проверка безопасности",
    "«Доступ с Вашего IP временно ограничен»": r"доступ с вашего ip временно ограничен",
    "«я не робот»": r"(?:я\s+не\s+робот|not\s+a\s+robot)",
    "firewall": r"firewall",
}


@dataclass
class ProbeResult:
    name: str
    url: str
    status: Optional[int] = None
    final_url: Optional[str] = None
    redirects: int = 0
    elapsed: Optional[float] = None
    size: Optional[int] = None
    title: Optional[str] = None
    markers: List[str] = field(default_factory=list)
    has_catalog: Optional[bool] = None
    error: Optional[str] = None


@dataclass
class IpInfo:
    ip: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    org: Optional[str] = None
    error: Optional[str] = None


def _session() -> requests.Session:
    s = requests.Session()
    # Только явно заданный маршрут, как у мониторинга; переменные окружения не подмешиваем
    s.trust_env = False
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })
    if settings.proxy_url:
        s.proxies = {"http": settings.proxy_url, "https": settings.proxy_url}
    return s


def _strip_query(url: Optional[str]) -> Optional[str]:
    """Конечный URL без параметров после «?» — в них могут быть одноразовые идентификаторы."""
    if not url:
        return url
    base, _, query = url.partition("?")
    return base + ("?…" if query else "")


def find_markers(html: str) -> List[str]:
    low = (html or "").lower()
    return [name for name, pattern in ROBOT_MARKERS.items() if re.search(pattern, low)]


def probe(session: requests.Session, name: str, url: str, check_catalog: bool = False) -> ProbeResult:
    """Один GET-запрос. Повторов нет."""
    result = ProbeResult(name=name, url=_strip_query(url))
    start = time.monotonic()
    try:
        resp = session.get(url, timeout=settings.request_timeout, allow_redirects=True)
    except requests.RequestException as e:
        result.elapsed = time.monotonic() - start
        result.error = redact(f"{type(e).__name__}: {str(e)[:160]}")
        return result
    result.elapsed = time.monotonic() - start
    html = resp.text or ""
    result.status = resp.status_code
    result.final_url = _strip_query(resp.url)
    result.redirects = len(resp.history)
    result.size = len(resp.content or b"")
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        result.title = " ".join(m.group(1).split())[:120]
    result.markers = find_markers(html)
    if check_catalog:
        result.has_catalog = ("catalog-serp" in html) or ("data-mfe-state" in html and "sortTimeStamp" in html)
    return result


def external_ip(session: requests.Session) -> IpInfo:
    """Один запрос к обычному сервису определения IP."""
    try:
        resp = session.get(IP_SERVICE, timeout=15, headers={"Accept": "application/json"})
        data: Dict = resp.json()
        return IpInfo(ip=data.get("ip"), country=data.get("country"), city=data.get("city"),
                      org=data.get("org"))
    except Exception as e:  # noqa: BLE001
        return IpInfo(error=redact(f"{type(e).__name__}: {str(e)[:120]}"))


def run_diagnostics(search_url: Optional[str]) -> Dict:
    session = _session()
    try:
        ip = external_ip(session)
        home = probe(session, "Главная Avito", AVITO_HOME)
        search = probe(session, "Первая сохранённая ссылка поиска", search_url, check_catalog=True) \
            if search_url else None
    finally:
        session.close()
    return {"ip": ip, "home": home, "search": search, "proxy": bool(settings.proxy_url)}
