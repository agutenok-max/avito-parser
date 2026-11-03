"""Запуск на Android (Termux): без lxml и без Playwright."""
import time

from services import avito_processor
from services.avito_fetcher import AvitoFetcher, ERROR
from services.avito_processor import AvitoProcessor
from tests.fixtures import card, mfe_item, mfe_page, page

NOW_MS = int(time.time() * 1000)


def _parse_both(html, url="https://www.avito.ru/moskva/velosipedy"):
    results = {}
    for parser in ("lxml", "html.parser"):
        avito_processor.HTML_PARSER = parser
        try:
            results[parser] = AvitoProcessor(url).process_html(html)
        finally:
            avito_processor.HTML_PARSER = "lxml"
    return results


def test_builtin_parser_gives_same_listings_as_lxml():
    items = [mfe_item(7600000001, "Велосипед Stels", NOW_MS - 120_000),
             mfe_item(7600000002, "Самокат Xiaomi", NOW_MS - 3_600_000)]
    res = _parse_both(mfe_page(items))
    fields = lambda rs: [(r.item_id, r.title, r.price, r.address, r.published_ts, r.url, r.images) for r in rs]
    assert len(res["html.parser"]) == 2
    assert fields(res["html.parser"]) == fields(res["lxml"])


def test_builtin_parser_html_cards_only():
    res = _parse_both(page([card(1000001, "Велосипед BMX"), card(1000002, "Велосипед Stels")]))
    assert [r.item_id for r in res["html.parser"]] == [r.item_id for r in res["lxml"]] == ["1000001", "1000002"]


def test_browser_missing_is_handled(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_playwright(name, *a, **k):
        if name.startswith("playwright"):
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    f = AvitoFetcher(mode="browser")
    r = f.fetch("https://www.avito.ru/moskva/velosipedy?s=104")
    assert r.status == ERROR and "Playwright" in r.error
