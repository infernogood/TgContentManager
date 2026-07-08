"""
scraper/github_collector.py
===========================
Чтение релизов GitHub через REST API v3 (aiohttp).

Поле `source.identifier` — репозиторий в формате "owner/repo".
Поле `extra` — необязательный JSON; если есть {"token": "..."} — он
перекрывает дефолтный github_token из настроек (например, для приватных
репозиториев). По умолчанию токен берётся из Settings.

Документация: https://docs.github.com/en/rest/releases/releases

Контракт:
    * 1 релиз = 1 CollectedItem.
    * raw_text = "tag: {tag}\n{name}\n\n{body}" (markdown).
    * source_url = html_url релиза (открывается в браузере).
    * Медиа нет (у релизов есть только assets — бинарники, их в TG не шлём).
"""
from __future__ import annotations

import logging

import aiohttp

from db.models import SourceType, Sources
from scraper.base import BaseCollector, CollectedItem
from services.settings_service import SettingsService

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
DEFAULT_LIMIT = 5  # кол-во последних релизов


class GithubCollector(BaseCollector):
    """Коллектор релизов GitHub."""

    handled_types = (SourceType.GITHUB,)

    def __init__(self, settings: SettingsService) -> None:
        super().__init__(settings)

    async def collect(self, source: Sources, owner_id: int) -> list[CollectedItem]:
        repo = source.identifier.strip().strip("/")
        # Токен берётся ИЗ НАСТРОЕК КОНКРЕТНОГО ЮЗЕРА (multi-user).
        token = await self.settings.get_str(owner_id, "github_token")

        url = f"{API_BASE}/repos/{repo}/releases"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "TgContentManager/1.0",
        }
        if token:
            # Токен повышает rate-limit с 60 до 5000 запросов/час.
            headers["Authorization"] = f"Bearer {token}"

        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 404:
                        log.warning("GitHub: репозиторий %s не найден", repo)
                        return []
                    if resp.status != 200:
                        log.warning("GitHub %s: HTTP %s", repo, resp.status)
                        return []
                    releases = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("GitHub %s: сетевой сбой: %s", repo, exc)
            return []

        items: list[CollectedItem] = []
        for rel in releases[:DEFAULT_LIMIT]:
            # draft=true и prerelease=true обычно пропускаем (они не анонсированы).
            if rel.get("draft") or rel.get("prerelease"):
                continue

            tag = rel.get("tag_name", "")
            name = rel.get("name") or tag
            body = rel.get("body") or ""
            html_url = rel.get("html_url") or f"https://github.com/{repo}/releases"

            raw_text = f"📦 Релиз {name}\nТег: {tag}\n\n{body}".strip()
            if len(raw_text) < 30:
                continue

            items.append(
                CollectedItem(
                    source_id=source.id,
                    source_url=html_url,
                    raw_text=raw_text,
                )
            )

        log.info("GitHub %s: собрано %d релизов", repo, len(items))
        return items
