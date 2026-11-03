"""
Загрузка страниц поиска Avito для мониторинга.

Принципы:
* Никаких попыток обойти CAPTCHA, авторизацию или защиту Avito.
  Если сайт показывает страницу «Доступ ограничен» / капчу — возвращаем
  статус BLOCKED, а монитор делает паузу. Отпечаток браузера не подменяется,
  капча не решается, прокси не ротируется.
* Сначала обычный HTTP-запрос (дёшево по памяти и трафику). Если страница
  пришла, но без списка объявлений (например, требуется JavaScript) — при
  FETCH_MODE=auto пробуем обычный headless-браузер Chromium (Playwright).
  При блокировке браузер не запускается: с того же IP он получит тот же отказ.
* Повтор запроса делается ТОЛЬКО при временной сетевой ошибке (сброс соединения,
  таймаут DNS и т.п.), не при блокировке — чтобы не нагружать Avito.
"""
import time
from dataclasses import dataclass
from typing import Optional

import requests

from config.settings import logger, settings

# Статусы результата
OK = "ok"
BLOCKED = "blocked"
ERROR = "error"
EMPTY = "empty"      # страница получена, объявлений нет («ничего не найдено») — это не ошибка

# Причина блокировки (для логов и /diagnose)
REASON_CAPTCHA = "captcha"
REASON_FORBIDDEN = "forbidden"      # HTTP 403 / «Доступ ограничен»
REASON_RATE_LIMIT = "rate_limit"    # HTTP 429 / 439 — слишком много запросов

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# Признаки именно CAPTCHA / проверки робота
_CAPTCHA_MARKERS = ("geetest", "hcaptcha", "recaptcha", "captcha", "проверка безопасности",
                    "firewall-container", "i'm not a robot", "я не робот")
# Признаки ограничения доступа по IP
_FORBIDDEN_MARKERS = ("доступ ограничен", "проблема с ip", "доступ с вашего ip временно ограничен")

_BLOCK_MARKERS = _CAPTCHA_MARKERS + _FORBIDDEN_MARKERS

# Сетевые ошибки, которые имеет смысл повторить один раз (временные)
_TRANSIENT_ERRORS = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)


@dataclass
class FetchResult:
    status: str
    html: Optional[str] = None
    http_status: Optional[int] = None
    method: str = ""
    error: Optional[str] = None
    elapsed: float = 0.0
    block_reason: Optional[str] = None   # REASON_* при status == BLOCKED
    attempts: int = 1                    # сколько сетевых попыток понадобилось


def block_reason(html: Optional[str], http_status: Optional[int]) -> Optional[str]:
    """Определяет причину блокировки или None, если блокировки нет."""
    head = (html or "")[:20000].lower()
    if any(m in head for m in _CAPTCHA_MARKERS) and "catalog-serp" not in (html or ""):
        return REASON_CAPTCHA
    if http_status in (429, 439):
        return REASON_RATE_LIMIT
    if http_status == 403:
        return REASON_FORBIDDEN
    if "catalog-serp" in (html or ""):
        return None
    if any(m in head for m in _FORBIDDEN_MARKERS):
        return REASON_FORBIDDEN
    return None


def looks_blocked(html: Optional[str], http_status: Optional[int] = None) -> bool:
    return block_reason(html, http_status) is not None


def looks_like_catalog(html: Optional[str]) -> bool:
    if not html:
        return False
    if "catalog-serp" in html or "__initialData__" in html:
        return True
    if "data-mfe-state" in html and "sortTimeStamp" in html:
        return True
    # Пустой результат поиска — это нормальная страница, просто без объявлений
    return "ничего не найдено" in html.lower()


def is_empty_result(html: Optional[str]) -> bool:
    """Страница-каталог без объявлений (валидный пустой поиск)."""
    if not html:
        return False
    low = html.lower()
    if "ничего не найдено" in low or "по вашему запросу ничего не найдено" in low:
        return "data-marker=\"item\"" not in html
    return False


def _reason_text(reason: Optional[str], http_status: Optional[int]) -> str:
    base = {
        REASON_CAPTCHA: "Avito показал проверку робота (CAPTCHA)",
        REASON_RATE_LIMIT: "Avito ограничил частоту запросов",
        REASON_FORBIDDEN: "Avito ограничил доступ с этого IP",
    }.get(reason, "Avito ограничил доступ")
    return f"{base} (HTTP {http_status})" if http_status else base


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
        if result.status in (OK, EMPTY) or self.mode == "http":
            return result
        # Блокировка по IP: браузер с того же адреса тоже получит отказ (проверено),
        # поэтому не тратим память и не создаём лишнюю нагрузку на Avito.
        if result.status == BLOCKED:
            return result

        # auto: пробуем браузер, если HTTP не дал каталог (например, нужен JS)
        browser = self._fetch_browser(url)
        if browser.status in (OK, EMPTY):
            return browser
        if BLOCKED in (result.status, browser.status):
            src = result if result.status == BLOCKED else browser
            return FetchResult(BLOCKED, http_status=src.http_status, method=f"{result.method}+{browser.method}",
                               error=src.error, block_reason=src.block_reason)
        return browser if browser.error and not result.error else result

    # -------------------------------------------------------------- http
    def _fetch_http(self, url: str) -> FetchResult:
        start = time.time()
        attempts = 0
        last_exc = None
        # Один повтор при временной сетевой ошибке; при блокировке повторов нет.
        for attempts in range(1, 3):
            try:
                resp = self.session.get(url, timeout=settings.request_timeout, allow_redirects=True)
                break
            except _TRANSIENT_ERRORS as e:
                last_exc = e
                if attempts < 2:
                    time.sleep(2)
                    continue
            except requests.RequestException as e:
                return FetchResult(ERROR, method="http", error=f"Ошибка соединения: {type(e).__name__}",
                                   elapsed=time.time() - start, attempts=attempts)
        else:
            return FetchResult(ERROR, method="http", error=f"Сетевая ошибка: {type(last_exc).__name__}",
                               elapsed=time.time() - start, attempts=attempts)

        html = resp.text
        elapsed = time.time() - start
        reason = block_reason(html, resp.status_code)
        if reason:
            return FetchResult(BLOCKED, html=None, http_status=resp.status_code, method="http",
                               error=_reason_text(reason, resp.status_code), elapsed=elapsed,
                               block_reason=reason, attempts=attempts)
        if resp.status_code == 404:
            return FetchResult(ERROR, http_status=404, method="http",
                               error="Страница не найдена (404). Проверьте ссылку.", elapsed=elapsed, attempts=attempts)
        if resp.status_code >= 400:
            return FetchResult(ERROR, http_status=resp.status_code, method="http",
                               error=f"Avito ответил ошибкой HTTP {resp.status_code}", elapsed=elapsed, attempts=attempts)
        if is_empty_result(html):
            return FetchResult(EMPTY, html=html, http_status=resp.status_code, method="http",
                               elapsed=elapsed, attempts=attempts)
        if not looks_like_catalog(html):
            return FetchResult(ERROR, html=html, http_status=resp.status_code, method="http",
                               error="Страница загрузилась, но список объявлений не найден", elapsed=elapsed,
                               attempts=attempts)
        return FetchResult(OK, html=html, http_status=resp.status_code, method="http", elapsed=elapsed,
                           attempts=attempts)

    # ----------------------------------------------------------- browser
    def _fetch_browser(self, url: str) -> FetchResult:
        start = time.time()
        if self._browser_available is False:
            return FetchResult(ERROR, method="browser", error="Браузер недоступен")
        try:
            from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright
        except ImportError:
            self._browser_available = False
            return FetchResult(ERROR, method="browser", error="Playwright не установлен")

        timed_out = False
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
                                                  user_agent=USER_AGENT,
                                                  viewport={"width": 1366, "height": 768})
                    context.set_default_navigation_timeout(60000)
                    page = context.new_page()
                    resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
                    status = resp.status if resp else None
                    # Ждём появления динамического содержимого: список объявлений ИЛИ
                    # встроенный JSON Avito. Любой из признаков означает, что страница догрузилась.
                    try:
                        page.wait_for_function(
                            """() => document.querySelector("div[data-marker='catalog-serp']")
                                   || document.querySelector("script[data-mfe-state='true']")
                                   || /ничего не найдено/i.test(document.body.innerText)""",
                            timeout=20000,
                        )
                    except PWTimeout:
                        timed_out = True
                    html = page.content()
                finally:
                    browser.close()
        except PWTimeout:
            return FetchResult(ERROR, method="browser", error="Таймаут загрузки страницы (60 с)",
                               elapsed=time.time() - start)
        except Exception as e:
            msg = str(e).split("\n")[0][:150]
            logger.debug(f"Ошибка браузера: {e}")
            return FetchResult(ERROR, method="browser", error=f"Ошибка браузера: {msg}",
                               elapsed=time.time() - start)

        elapsed = time.time() - start
        reason = block_reason(html, status)
        if reason:
            return FetchResult(BLOCKED, http_status=status, method="browser",
                               error=_reason_text(reason, status), elapsed=elapsed, block_reason=reason)
        if is_empty_result(html):
            return FetchResult(EMPTY, html=html, http_status=status, method="browser", elapsed=elapsed)
        if not looks_like_catalog(html):
            err = "Таймаут ожидания объявлений" if timed_out else "Страница загрузилась, но список объявлений не найден"
            return FetchResult(ERROR, html=html, http_status=status, method="browser", error=err, elapsed=elapsed)
        return FetchResult(OK, html=html, http_status=status, method="browser", elapsed=elapsed)
