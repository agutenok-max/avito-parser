from services.url_tools import extract_url, normalize_search_url, search_title_from_url


def test_adds_sort_by_date_and_normalizes_host():
    r = normalize_search_url("https://m.avito.ru/moskva/velosipedy?q=bmx&s=1&p=3")
    assert r.ok
    assert r.url.startswith("https://www.avito.ru/moskva/velosipedy?")
    assert "s=104" in r.url and "p=3" not in r.url and "s=1&" not in r.url


def test_extracts_url_from_shared_text():
    text = "Посмотрите на Авито: https://www.avito.ru/moskva/velosipedy?q=bmx, круто"
    assert extract_url(text) == "https://www.avito.ru/moskva/velosipedy?q=bmx"
    assert normalize_search_url(text).ok


def test_rejects_non_avito_and_item_pages():
    assert not normalize_search_url("https://example.com/x").ok
    assert not normalize_search_url("просто текст").ok
    r = normalize_search_url("https://www.avito.ru/moskva/velosipedy/bmx_1234567890")
    assert not r.ok and "одно объявление" in r.error
    assert not normalize_search_url("https://www.avito.ru/").ok


def test_title():
    assert search_title_from_url("https://www.avito.ru/moskva/velosipedy?q=bmx+pro&s=104") == "bmx pro (moskva)"
    assert "videokarty" in search_title_from_url(
        "https://www.avito.ru/all/tovary_dlya_kompyutera/komplektuyuschie/videokarty-ASgBAgICAkTGB~pm7gmmZw?s=104")
