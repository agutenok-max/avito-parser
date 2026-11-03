"""Синтетические страницы Avito для тестов (структура как у реальной выдачи)."""
import json
from urllib.parse import quote


def card(item_id, title, price=1000, date="5 минут назад", city="moskva", img=True):
    image = (f'<div data-marker="item-photo"><img src="https://img.avito.st/s/{item_id}.jpg" '
             f'srcset="https://img.avito.st/s/{item_id}_1.jpg 208w, https://img.avito.st/s/{item_id}_2.jpg 636w"></div>'
             if img else "")
    return f"""
    <div data-marker="item" data-item-id="{item_id}">
      {image}
      <a data-marker="item-title" href="/{city}/velosipedy/{title.lower().replace(' ', '_')}_{item_id}" title="{title}">{title}</a>
      <meta itemprop="price" content="{price}">
      <meta itemprop="description" content="Описание {title} &amp; детали">
      <div class="geo-root-abc"><span>Москва, м. Тверская</span></div>
      <p data-marker="item-date">{date}</p>
    </div>"""


def page(cards, initial=None):
    script = ""
    if initial is not None:
        script = f'<script>window.__initialData__ = "{quote(json.dumps(initial))}";</script>'
    return f"""<html><head><title>Велосипеды</title></head><body>
    <div data-marker="catalog-serp">{''.join(cards)}</div>{script}</body></html>"""


BLOCK_PAGE = "<html><head><title>Доступ ограничен: проверка безопасности</title></head><body>captcha</body></html>"


def mfe_item(item_id, title, ts_ms, price=5000, city="moskva", category="velosipedy", images=True, promoted=False):
    item = {
        "id": item_id, "title": title, "urlPath": f"/{city}/{category}/{title.lower().replace(' ', '_')}_{item_id}",
        "sortTimeStamp": ts_ms, "description": f"Описание {title}",
        "priceDetailed": {"enabled": True, "fullString": f"{price:,}".replace(",", " ") + " ₽",
                          "string": f"{price} ₽", "value": price, "hasValue": True},
        "location": {"id": 637640, "name": "Москва"},
        "geo": {"formattedAddress": "Москва, ул. Тверская, 1", "geoReferences": []},
        "images": [{"208x156": f"https://00.img.avito.st/image/1/{item_id}.small",
                    "636x476": f"https://00.img.avito.st/image/1/{item_id}.big"}] if images else [],
    }
    if promoted:
        item["iva"] = {"DateInfoStep": [{"payload": {"vas": [{"title": "Продвинуто"}]}}]}
    return item


def mfe_page(items, with_cards=True):
    """Страница в актуальном формате Avito: JSON в <script type="mime/invalid" data-mfe-state="true">."""
    import html as html_lib
    state = {"i18n": {"hasMessages": True}, "loaderData": {"data": {"catalog": {"items": items}}}}
    script = (f'<script type="mime/invalid" data-mfe-state="true">'
              f'{html_lib.escape(json.dumps(state, ensure_ascii=False))}</script>')
    cards = ""
    if with_cards:
        cards = "".join(
            f'<div data-marker="item" data-item-id="{i["id"]}">'
            f'<a data-marker="item-title" href="{i["urlPath"]}?context=abc" title="{i["title"]}">{i["title"]}</a>'
            f'<meta itemprop="price" content="{i["priceDetailed"]["value"]}">'
            f'<div class="geo-root-x"><span>Москва, Тверская</span><p data-marker="item-date">Вчера</p></div>'
            f'</div>' for i in items)
    return (f'<html><head>{script}</head><body><div data-marker="catalog-serp">{cards}</div>'
            f'</body></html>')
