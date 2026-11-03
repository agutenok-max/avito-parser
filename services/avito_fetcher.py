"""
Загрузка страниц поиска Avito для мониторинга.

Принципы:
* Никаких попыток обойти CAPTCHA, авторизацию или защиту Avito.
  Если сайт показывает страницу «Доступ ограничен» / капчу — возвращаем
  статус BLOCKED, а монитор делает паузу.
* Сначала обычный HTTP-запрос (дёшево по памяти и трафику). Если он не
  вернул страницу каталога — при FETCH_MODE=auto пробуем обычный
  headless-браузер Chromium (Playwright), который просто открывает страницу.
"""
import time
from dataclasses import dataclass
from typing import Optional

import requests

from config.settings import logger, settings

OK = "ok"
BLOCKED = "blocked"
ERROR = "error"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

_BLOCK_MARKERS = (
    "доступ ограничен",
    "проблема с ip",
    "доступ с вашего ip временно ограничен",
    "проверка безопасности",
    "firewall-container",
    "geetest",
    "hcaptcha",
    "captcha",
)


@dataclass
class FetchResult:
    status: str
    html: Optional[str] = None
    http_status: Optional[int] = None
    method: str = ""
    error: Optional[str] = None
    elapsed: float = 0.0


def looks_blocked(html: Optional[str], http_status: Optional[int] = None) -> bool:
    if http_status in (403, 429, 439):
        return True
    if not html:
        return False
    head = html[:20000].lower()
    if "catalog-serp" in html:
        return False
    return any(marker in head for marker in _BLOCK_MARKERS)


def looks_like_catalog(html: Optional[str]) -> bool:
    if not html:
        return False
    if "catalog-serp" in html or "__initialData__" in html:
        return True
    # Пустой результат поиска — это нормальная страница, просто без объявлений
    return "ничего не найдено" in html.lower()


class AvitoFetcher:
    def __init__(self, mode: Optional[str] = None):
        self.mode = (mode or settings.fetch_mode or "auto").lower()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        })
        if settings.proxy_url:
            self.session.proxies = {"http": settings.proxy_url, "https": settings.proxy_url}
        self._browser_available: Optional[bool] = None

    # ------------------------------------------------------------ public
    def fetch(self, url: str) -> FetchResult:
        if self.mode == "browser":
            return self._fetch_browser(url)

        result = self._fetch_http(url)
        if result.status == OK or self.mode == "http":
            return result

        # auto: пробуем браузер, если HTTP не дал каталог
        browser = self._fetch_browser(url)
        if browser.status == OK:
            return browser
        # Если хоть один способ показал блокировку — сообщаем о блокировке
        if BLOCKED in (result.status, browser.status):
            return FetchResult(BLOCKED, http_status=result.http_status or browser.http_status,
                               method=f"{result.method}+{browser.method}",
                               error=result.error if result.status == BLOCKED else browser.error)
        return browser if browser.error and not result.error else result

    # -------------------------------------------------------------- http
    def _fetch_http(self, url: str) -> FetchResult:
        start = time.time()
        try:
            resp = self.session.get(url, timeout=settings.request_timeout, allow_redirects=True)
        except requests.RequestException as e:
            return FetchResult(ERROR, method="http", error=f"Ошибка соединения: {type(e).__name__}",
                               elapsed=time.time() - start)
        html = resp.text
        elapsed = time.time() - start
        if looks_blocked(html, resp.status_code):
            return FetchResult(BLOCKED, html=None, http_status=resp.status_code, method="http",
                               error=f"Avito ограничил доступ (HTTP {resp.status_code})", elapsed=elapsed)
        if resp.status_code == 404:
            return FetchResult(ERROR, http_status=404, method="http",
                               error="Страница не найдена (404). Проверьте ссылку.", elapsed=elapsed)
        if resp.status_code >= 400:
            return FetchResult(ERROR, http_status=resp.status_code, method="http",
                               error=f"Avito ответил ошибкой HTTP {resp.status_code}", elapsed=elapsed)
        if not looks_like_catalog(html):
            return FetchResult(ERROR, html=html, http_status=resp.status_code, method="http",
                               error="Страница загрузилась, но список объявлений не найден", elapsed=elapsed)
        return FetchResult(OK, html=html, http_status=resp.status_code, method="http", elapsed=elapsed)

    # ----------------------------------------------------------- browser
    def _fetch_browser(self, url: str) -> FetchResult:
        start = time.time()
        if self._browser_available is False:
            return FetchResult(ERROR, method="browser", error="Браузер недоступен")
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            self._browser_available = False
            return FetchResult(ERROR, method="browser", error="Playwright не установлен")

        try:
            with sync_playwright() as p:
                launch_kwargs = {"headless": True, "args": ["--disable-dev-shm-usage", "--no-sandbox"]}
                if settings.proxy_url:
                    launch_kwargs["proxy"] = {"server": settings.proxy_url}
                try:
                    browser = p.chromium.launch(**launch_kwargs)
                except Exception as e:
                    self._browser_available = False
                    logger.warning(f"Браузер Chromium не запускается: {e}")
                    return FetchResult(ERROR, method="browser", error="Браузер недоступен")
                self._browser_available = True
                try:
                    context = browser.new_context(locale="ru-RU", timezone_id="Europe/Moscow",
                                                  user_agent=USER_AGENT)
                    page = context.new_page()
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    status = resp.status if resp else None
                    try:
                        page.wait_for_selector("div[data-marker='catalog-serp']", timeout=15000)
                    except PWTimeout:
                        pass
                    html = page.content()
                finally:
                    browser.close()
        except Exception as e:
            msg = str(e).split("\n")[0][:150]
            logger.debug(f"Ошибка браузера: {e}")
            return FetchResult(ERROR, method="browser", error=f"Ошибка браузера: {msg}",
                               elapsed=time.time() - start)

        elapsed = time.time() - start
        if looks_blocked(html, status):
            return FetchResult(BLOCKED, http_status=status, method="browser",
                               error=f"Avito ограничил доступ (HTTP {status})", elapsed=elapsed)
        if not looks_like_catalog(html):
            return FetchResult(ERROR, html=html, http_status=status, method="browser",
                               error="Страница загрузилась, но список объявлений не найден", elapsed=elapsed)
        return FetchResult(OK, html=html, http_status=status, method="browser", elapsed=elapsed)
