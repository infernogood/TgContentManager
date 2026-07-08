"""
scraper/rss_collector.py
========================
Парсинг RSS/Atom-фидов через feedparser.

Главный кейс — Reddit (.rss). feedparser УМЕЕТ работать async-обёрткой,
но сам по себе синхронный; оборачиваем в asyncio.to_thread, чтобы не
блокировать event loop на CPU-парсинге фида.

Поле `source.identifier` — полный URL фида,
напр. "https://www.reddit.com/r/Python.rss".

Извлекаем:
    * title + summary = raw_text
    * link = source_url
    * картинки (из media_thumbnail / media_content / enclosures) = media_urls
"""
from __future__ import annotations

import asyncio
import logging

import feedparser

from db.models import SourceType, Sources
from scraper.base import BaseCollector, CollectedItem
from services.settings_service import SettingsService

log = logging.getLogger(__name__)

# Лимит записей на один фид (feedparser возвращает ~25 последних, но подстраховываемся).
DEFAULT_LIMIT = 15

# Минимальная длина текста, чтобы не плодить пустые карточки.
MIN_TEXT_LENGTH = 30


class RssCollector(BaseCollector):
    """RSS/Atom-коллектор на feedparser."""

    handled_types = (SourceType.RSS,)

    def __init__(self, settings: SettingsService) -> None:
        super().__init__(settings)

    async def collect(self, source: Sources, owner_id: int) -> list[CollectedItem]:
        feed_url = source.identifier.strip()
        # feedparser — синхронный. Уводим в thread.
        parsed = await asyncio.to_thread(feedparser.parse, feed_url)

        if parsed.bozo and not parsed.entries:
            log.warning("RSS %s: фид не распарсен (%s)", feed_url, getattr(parsed, "bozo_exception", "?"))
            return []

        items: list[CollectedItem] = []
        for entry in parsed.entries[:DEFAULT_LIMIT]:
            title = (entry.get("title") or "").strip()
            summary = (entry.get("summary") or entry.get("description") or "").strip()
            link = (entry.get("link") or feed_url).strip()

            # Склеиваем заголовок и анонс в сырой текст.
            raw_text = (title + "\n\n" + summary).strip() if title and summary else (title or summary)
            if len(raw_text) < MIN_TEXT_LENGTH:
                continue

            media_urls = self._extract_image_urls(entry)
            media_type = "photo" if media_urls else "text"

            items.append(
                CollectedItem(
                    source_id=source.id,
                    source_url=link,
                    raw_text=raw_text,
                    media_urls=media_urls,
                    media_type_hint=media_type,
                )
            )

        log.info("RSS %s: собрано %d элементов", feed_url, len(items))
        return items

    @staticmethod
    def _extract_image_urls(entry) -> list[str]:
        """Достаёт URL картинок из feedparser-entry (разные фиды прячут их по-разному)."""
        urls: list[str] = []

        # 1. media_content (наиболее частый вариант у Reddit).
        for media in entry.get("media_content", []) or []:
            url = media.get("url")
            if url and url not in urls:
                urls.append(url)

        # 2. media_thumbnail.
        for media in entry.get("media_thumbnail", []) or []:
            url = media.get("url")
            if url and url not in urls:
                urls.append(url)

        # 3. enclosures (например, podcast covers).
        for enc in entry.get("enclosures", []) or []:
            href = enc.get("href")
            ctype = (enc.get("type") or "").lower()
            if href and ctype.startswith("image/") and href not in urls:
                urls.append(href)

        # Берём максимум одну картинку: если в посте несколько, шлём первую —
        # иначе Telegram-сообщение станет媒体-группой, что усложняет модерацию.
        return urls[:1]
