from datetime import datetime

from services.avito_processor import AvitoProcessor, item_id_from_url, parse_relative_date
from tests.fixtures import card, page


def test_parses_cards():
    html = page([card(1000001, "Велосипед BMX", 15000), card(1000002, "Самокат", 3000, img=False)])
    items = AvitoProcessor("https://www.avito.ru/moskva/velosipedy").process_html(html)
    assert [i.item_id for i in items] == ["1000001", "1000002"]
    first = items[0]
    assert first.title == "Велосипед BMX"
    assert first.price == "15 000 ₽"
    assert first.url == "https://www.avito.ru/moskva/velosipedy/велосипед_bmx_1000001"
    assert first.address == "Москва, м. Тверская"
    assert first.published_text == "5 минут назад"
    assert first.images == ["https://img.avito.st/s/1000001_2.jpg"]
    assert "детали" in first.description
    assert items[1].images == []


def test_stop_words_disabled_by_default_and_optional():
    html = page([card(1000001, "Продажа велосипеда")])
    assert len(AvitoProcessor("https://www.avito.ru").process_html(html)) == 1
    assert AvitoProcessor("https://www.avito.ru", stop_words=["продажа"]).process_html(html) == []


def test_initial_data_timestamps_and_json_fallback():
    initial = {"data": {"catalog": {"items": [
        {"id": 1000001, "title": "Велосипед BMX", "urlPath": "/moskva/velosipedy/bmx_1000001",
         "sortTimeStamp": 1762160000000, "priceDetailed": {"value": 15000}},
    ]}}}
    items = AvitoProcessor("https://www.avito.ru").process_html(page([card(1000001, "Велосипед BMX")], initial))
    assert items[0].published_ts == 1762160000
    # Без HTML-карточек используем только JSON
    only_json = AvitoProcessor("https://www.avito.ru").process_html(
        page([], initial).replace('<div data-marker="catalog-serp"></div>', ""))
    assert only_json[0].item_id == "1000001" and only_json[0].price == "15 000 ₽"


def test_no_catalog_returns_empty():
    assert AvitoProcessor("https://www.avito.ru").process_html("<html><body>nothing</body></html>") == []


def test_item_id_from_url():
    assert item_id_from_url("https://www.avito.ru/moskva/velosipedy/bmx_1234567890") == "1234567890"
    assert item_id_from_url("https://www.avito.ru/moskva/velosipedy") is None


def test_relative_dates():
    now = datetime(2026, 11, 3, 15, 0)
    assert parse_relative_date("5 минут назад", now) == (datetime(2026, 11, 3, 14, 55), True)
    assert parse_relative_date("Вчера 12:30", now) == (datetime(2026, 11, 2, 12, 30), True)
    assert parse_relative_date("2 октября 14:00", now) == (datetime(2026, 10, 2, 14, 0), True)
    assert parse_relative_date("28 декабря", datetime(2026, 1, 5)) == (datetime(2025, 12, 28), False)
    dt, exact = parse_relative_date("3 часа назад", now)
    assert dt == datetime(2026, 11, 3, 12, 0) and exact is False
    assert parse_relative_date("непонятно", now) == (None, False)
