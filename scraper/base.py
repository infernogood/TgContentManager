"""
scraper/base.py
===============
Доменная модель и абстрактный базовый класс всех коллекторов.

CollectedItem — что возвращает коллектор. Один источник за один прогон
может вернуть несколько CollectedItem'ов.

Поле media может быть представлено ДВУМЯ способами:
    * media_urls — список URL (HTTP), pipeline сам скачает.
    * media_paths — список уже скачанных локальных файлов (Path).
      Используется TelegramCollector'ом: Telethon качает медиа напрямую,
      минуя HTTP, т.к. у Telegram-файлов нет публичных URL.

Если оба списка пусты — пост уйдёт как чистый текст.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from db.models import Sources
from services.settings_service import SettingsService


@dataclass
class CollectedItem:
    """Единица собранного контента, готовая к пайплайну."""

    source_id: int | None
    source_url: str           # Для аудита/дедупа. Формат зависит от источника.
    raw_text: str             # Исходный текст (необработанный).
    media_urls: list[str] = field(default_factory=list)
    media_paths: list[Path] = field(default_factory=list)
    # Подсказка для пайплайна, чем является медиа. Берётся по первому элементу.
    media_type_hint: str = "text"  # text | photo | video | animation

    @property
    def has_media(self) -> bool:
        return bool(self.media_urls) or bool(self.media_paths)


class BaseCollector(ABC):
    """
    Абстрактный коллектор.

    Каждый конкретный коллектор реализует только collect(). owner_id
    передаётся явно — это нужно коллекторам, берущим ключи из настроек
    конкретного юзера (GitHub/NewsData). RSS/Telethon его игнорируют,
    но сигнатура одинаковая для единообразия.
    """

    #: SourceType-ы, которые обрабатывает этот коллектор.
    handled_types: tuple = ()

    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings

    @abstractmethod
    async def collect(self, source: Sources, owner_id: int) -> list[CollectedItem]:
        """Собрать контент из источника от имени юзера owner_id."""
        raise NotImplementedError
