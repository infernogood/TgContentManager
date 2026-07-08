"""
scraper/newsdata_collector.py
=============================
Чтение новостей через NewsData.io API (aiohttp).

Поле `source.identifier` — поисковый запрос (параметр `q`),
напр. "AI OR \"machine learning\"".

Документация: https://newsdata.io/documentation

Контракт:
    * 1 статья = 1 CollectedItem.
    * raw_text = title + description + content (обрезанный).
    * media_urls = [image_url] если есть.
"""
from __future__ import annotations

import logging

import aiohttp

from db.models import SourceType, Sources
from scraper.base import BaseCollector, CollectedItem
from services.settings_service import SettingsService

log = logging.getLogger(__name__)

API_BASE = "https://newsdata.io/api/1/news"
DEFAULT_LIMIT = 10  # статей за один прогон (API и так отдаёт ~10)


class NewsdataCollector(BaseCollector):
    """Коллектор NewsData.io."""

    handled_types = (SourceType.NEWSDATA,)

    def __init__(self, settings: SettingsService) -> None:
        super().__init__(settings)

    async def collect(self, source: Sources, owner_id: int) -> list[CollectedItem]:
        query = source.identifier.strip()
        # API-ключ из настроек юзера.
        api_key = await self.settings.get_str(owner_id, "newsdata_api_key")
        if not api_key:
            log.warning("NewsData: нет API-ключа в настройках — пропуск.")
            return []

        params = {
            "apikey": api_key,
            "q": query,
            # language=en по умолчанию; при необходимости вынести в source.extra.
            "language": "en",
        }
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(API_BASE, params=params) as resp:
                    if resp.status != 200:
                        log.warning("NewsData %r: HTTP %s", query, resp.status)
                        return []
                    payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("NewsData %r: сетевой сбой: %s", query, exc)
            return []

        results = payload.get("results") or []
        items: list[CollectedItem] = []
        for art in results[:DEFAULT_LIMIT]:
            title = (art.get("title") or "").strip()
            description = (art.get("description") or "").strip()
            content = (art.get("content") or "").strip()
            link = (art.get("link") or "").strip()
            image_url = (art.get("image_url") or "").strip()

            raw_text = "\n\n".join(part for part in (title, description, content) if part)
            if len(raw_text) < 30:
                continue

            media_urls = [image_url] if image_url else []

            items.append(
                CollectedItem(
                    source_id=source.id,
                    source_url=link or f"newsdata://{query}",
                    raw_text=raw_text,
                    media_urls=media_urls,
                    media_type_hint="photo" if media_urls else "text",
                )
            )

        log.info("NewsData %r: собрано %d статей", query, len(items))
        return items
