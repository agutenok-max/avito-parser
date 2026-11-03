"""Актуальный формат страниц Avito (2025–2026) и разные категории."""
import time

from services.avito_fetcher import looks_blocked, looks_like_catalog
from services.avito_processor import AvitoProcessor
from services.url_tools import normalize_search_url, page_url
from tests.fixtures import mfe_item, mfe_page

NOW_MS = int(time.time() * 1000)


def test_mfe_state_json_with_cards_gives_exact_dates():
    items = [mfe_item(7600000001, "Велосипед Stels", NOW_MS - 120_000),
             mfe_item(7600000002, "Самокат Xiaomi", NOW_MS - 3_600_000)]
    res = AvitoProcessor("https://www.avito.ru/moskva/velosipedy").process_html(mfe_page(items))
    assert [r.item_id for r in res] == ["7600000001", "7600000002"]
    first = res[0]
    assert first.published_ts == (NOW_MS - 120_000) // 1000
    assert first.url == "https://www.avito.ru/moskva/velosipedy/велосипед_stels_7600000001"  # без ?context
    assert first.address == "Москва, Тверская"   # дата «Вчера» не прилипла к адресу
    assert first.published_text == "Вчера"
    assert first.images == ["https://00.img.avito.st/image/1/7600000001.big"]
    assert first.description == "Описание Велосипед Stels"


def test_mfe_state_json_only_without_cards():
    items = [mfe_item(7600000003, "iPhone 15", NOW_MS, price=65000, category="telefony")]
    html = mfe_page(items, with_cards=False)
    assert looks_like_catalog(html) and not looks_blocked(html, 200)
    res = AvitoProcessor("https://www.avito.ru/moskva/telefony").process_html(html)
    assert len(res) == 1
    r = res[0]
    assert r.title == "iPhone 15" and r.price == "65 000 ₽"
    assert r.address == "Москва, ул. Тверская, 1"
    assert r.published_ts == NOW_MS // 1000


def test_different_categories_and_filters_are_preserved():
    urls = {
        "https://www.avito.ru/moskva/kvartiry/sdam/na_dlitelnyy_srok-ASgBAgICAkSSA8gQ8AeQUg?cd=1&f=ASgBAQ&pmax=60000":
            ["pmax=60000", "f=ASgBAQ", "cd=1"],
        "https://www.avito.ru/rossiya/avtomobili/toyota/camry-ASgBAgICAkTgtg3cmCjitg2KmSg?radius=200&searchRadius=200":
            ["radius=200", "searchRadius=200"],
        "https://www.avito.ru/sankt-peterburg/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc?q=iphone+15&pmin=30000":
            ["q=iphone+15", "pmin=30000"],
        "https://www.avito.ru/all/vakansii?q=водитель": ["q=%D0%B2%D0%BE%D0%B4%D0%B8%D1%82%D0%B5%D0%BB%D1%8C"],
        "https://www.avito.ru/moskva/predlozheniya_uslug/remont_i_otdelka-ASgBAgICAUSYC8CfAQ": [],
        "https://m.avito.ru/ekaterinburg/bytovaya_elektronika?s=1&p=4": [],
    }
    for url, must in urls.items():
        r = normalize_search_url(url)
        assert r.ok, url
        assert r.url.endswith("s=104") and r.url.count("s=104") == 1, r.url
        assert "&p=" not in r.url and "?p=" not in r.url
        for part in must:
            assert part in r.url, (part, r.url)
        # путь (категория и фильтры в пути) не меняется
        assert r.url.split("?")[0].split("avito.ru")[1] == url.split("?")[0].split("avito.ru")[1]


def test_page_url():
    assert page_url("https://www.avito.ru/moskva/velosipedy?q=bmx&s=104", 2) == \
        "https://www.avito.ru/moskva/velosipedy?q=bmx&s=104&p=2"
    assert page_url("https://www.avito.ru/moskva/velosipedy?p=3&s=104", 1) == \
        "https://www.avito.ru/moskva/velosipedy?s=104"


def test_real_block_pages():
    ip_block = "<html><head><title>Доступ ограничен: проблема с IP</title></head><body>firewall</body></html>"
    assert looks_blocked(ip_block, 429)
    assert looks_blocked(ip_block, 200)
    assert looks_blocked("<html>...</html>", 439)


def test_auto_mode_does_not_launch_browser_when_blocked(monkeypatch):
    from services import avito_fetcher as af
    f = af.AvitoFetcher("auto")
    monkeypatch.setattr(f, "_fetch_http", lambda url: af.FetchResult(af.BLOCKED, http_status=429, method="http"))
    called = []
    monkeypatch.setattr(f, "_fetch_browser", lambda url: called.append(url) or af.FetchResult(af.OK, html="x"))
    assert f.fetch("u").status == af.BLOCKED and called == []
    # Страница без каталога (нужен JS) -> пробуем браузер
    monkeypatch.setattr(f, "_fetch_http", lambda url: af.FetchResult(af.ERROR, error="no catalog", method="http"))
    assert f.fetch("u").status == af.OK and called == ["u"]
