from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Listing:
    """Модель данных для одного объявления Avito."""
    url: str
    title: str
    price: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    images: List[str] = field(default_factory=list)

    # Дополнительные поля из урока
    bail: Optional[str] = None      # Залог
    tax: Optional[str] = None       # Комиссия
    services: Optional[str] = None  # ЖКУ

    # === Поля для мониторинга ===
    item_id: Optional[str] = None             # ID объявления на Avito
    published_ts: Optional[int] = None        # Unix-время публикации/поднятия (если известно точно)
    published_text: Optional[str] = None      # Текст даты с сайта, например «3 часа назад»
    address_from_url: bool = False            # адрес угадан по ссылке (город), а не взят со страницы
