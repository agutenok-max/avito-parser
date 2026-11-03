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
