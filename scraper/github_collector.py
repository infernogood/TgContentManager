"""
scraper/github_collector.py
===========================
Чтение релизов GitHub через REST API v3 (aiohttp).

Поле `source.identifier`:
  - Для режима репозитория: "owner/repo".
  - Для режима поиска: может быть "-" или пустым (используется как fallback).

Поле `source.extra` (JSON):
  - {"topics": ["llm", "python"]} — если задано, включается режим поиска
    по топикам. Используется Search API: `topic:llm topic:python`.
  - {"token": "..."} — токен для приватных репозиториев (перекрывает настройки).

Документация: https://docs.github.com/en/rest/releases/releases
            https://docs.github.com/en/rest/search/search

Контракт:
    * 1 релиз = 1 CollectedItem.
    * raw_text = "tag: {tag}\n{name}\n\n{body}" (markdown).
    * source_url = html_url релиза (открывается в браузере).
    * Медиа нет.
"""
from __future__ import annotations

import json
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
        token = await self.settings.get_str(owner_id, "github_token")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "TgContentManager/1.0",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        extra_topics: list[str] = []
        if source.extra:
            try:
                extra_data = json.loads(source.extra)
                extra_topics = extra_data.get("topics", [])
            except json.JSONDecodeError:
                log.warning("GitHub %s: неверный JSON в extra", source.id)

        if extra_topics:
            return await self._collect_by_topics(source, owner_id, headers, extra_topics)

        repo = source.identifier.strip().strip("/")
        if not repo or repo == "-":
            log.warning("GitHub %s: нет идентификатора репозитория и топиков", source.id)
            return []

        return await self._collect_by_repo(source, owner_id, headers, repo)

    async def _collect_by_repo(
        self, source: Sources, owner_id: int, headers: dict, repo: str
    ) -> list[CollectedItem]:
        url = f"{API_BASE}/repos/{repo}/releases"
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

    async def _collect_by_topics(
        self, source: Sources, owner_id: int, headers: dict, topics: list[str]
    ) -> list[CollectedItem]:
        query = " ".join([f"topic:{t}" for t in topics])
        url = f"{API_BASE}/search/repositories?q={query}&sort=updated&order=desc"
        timeout = aiohttp.ClientTimeout(total=20)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warning("GitHub Search: HTTP %s", resp.status)
                        return []
                    data = await resp.json()
                    repos = data.get("items", [])[:5]  # лимит 5 репозиториев
        except (aiohttp.ClientError, TimeoutError) as exc:
            log.warning("GitHub Search: сетевой сбой: %s", exc)
            return []

        items: list[CollectedItem] = []
        for repo_item in repos:
            full_name = repo_item.get("full_name")
            if not full_name:
                continue

            html_url = repo_item.get("html_url", f"https://github.com/{full_name}")
            desc = repo_item.get("description") or "Без описания"
            raw_text = f"🔥 New/Updated Repo: {full_name}\n\n{desc}"

            items.append(
                CollectedItem(
                    source_id=source.id,
                    source_url=html_url,
                    raw_text=raw_text,
                )
            )

        log.info("GitHub Search (%s): собрано %d репозиториев", query, len(items))
        return items
