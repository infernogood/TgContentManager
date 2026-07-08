"""
scraper/manager.py
==================
CollectorManager — оркестратор одного цикла сбора в multi-user среде.

Алгоритм run_once():
    1. Перечитывает кэш настроек (системный + всех юзеров).
    2. Берёт всех ACTIVE юзеров из БД.
    3. Для каждого юзера:
       a) Читает его активные источники.
       b) Фильтрует «дозревшие» по last_fetched_at (interval — из настроек юзера).
       c) Для каждого источника вызывает подходящий коллектор.
       d) CollectedItem отдаёт в PostsService.process_collected_item(owner_id=user.id).

Преимущество: один Telethon-клиент, один Bot на всех юзеров. Состояния
(сессии, ключи, промпты) — per-user через Settings.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from db.database import SessionFactory
from db.models import SourceType, Sources, UserStatus
from db.repositories import SourcesRepository, UsersRepository
from scraper.base import BaseCollector, CollectedItem
from scraper.github_collector import GithubCollector
from scraper.newsdata_collector import NewsdataCollector
from scraper.rss_collector import RssCollector
from scraper.telegram_collector import TelegramCollector
from services.posts_service import PostsService
from services.settings_service import SettingsService

if TYPE_CHECKING:
    from telethon import TelegramClient

log = logging.getLogger(__name__)

ModerationKbFactory = Callable[[int], InlineKeyboardMarkup]


class CollectorManager:
    """Фасад, маршрутизирующий источники по коллекторам для всех юзеров."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: SettingsService,
        posts_service: PostsService,
        telethon_client: "TelegramClient | None" = None,
        moderation_kb_factory: ModerationKbFactory | None = None,
        bot: Bot | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._posts_service = posts_service
        self._telethon_client = telethon_client
        self._moderation_kb_factory = moderation_kb_factory
        self._bot = bot

        # Реестр коллекторов: тип -> коллектор (ОБЩИЕ на всех юзеров).
        # Per-user параметры (api_key, token) коллектор берёт через SettingsService
        # по переданному owner_id. См., напр., github_collector.collect().
        self._collectors: dict[SourceType, BaseCollector] = {
            SourceType.RSS: RssCollector(settings),
            SourceType.GITHUB: GithubCollector(settings),
            SourceType.NEWSDATA: NewsdataCollector(settings),
        }
        if telethon_client is not None:
            self._collectors[SourceType.TG] = TelegramCollector(settings, telethon_client)
        else:
            log.warning("Telethon клиент не передан — TG-источники будут пропускаться.")

    def register_collector(self, source_type: SourceType, collector: BaseCollector) -> None:
        self._collectors[source_type] = collector

    # ------------------------------------------------------------------ #
    #  Главный цикл (multi-user)
    # ------------------------------------------------------------------ #
    async def run_once(self) -> int:
        """Один такт сбора по всем активным юзерам. Возвращает кол-во черновиков."""
        await self._settings.reload()

        # Список активных юзеров.
        async with self._session_factory() as session:
            urepo = UsersRepository(session)
            users = await urepo.list_active_for_collection()

        if not users:
            log.info("CollectorManager: нет активных юзеров — пропуск цикла.")
            return 0

        log.info("CollectorManager: старт цикла, активных юзеров: %d", len(users))
        created_total = 0

        for user in users:
            try:
                created = await self._run_for_user(user.id)
                created_total += created
            except Exception as exc:  # noqa: BLE001
                log.exception("Сбой при обработке юзера %s: %s", user.id, exc)

        log.info("CollectorManager: цикл завершён, всего черновиков: %d", created_total)
        return created_total

    async def _run_for_user(self, owner_id: int) -> int:
        """Прогон коллекторов для одного юзера."""
        # Интервал у этого юзера.
        interval_minutes = await self._settings.get_int(owner_id, "collector_interval_minutes", 60)
        cutoff = datetime.utcnow() - timedelta(minutes=interval_minutes)

        async with self._session_factory() as session:
            repo = SourcesRepository(session)
            all_sources = await repo.list_active(owner_id)

        # Фильтр «дозревших».
        sources = [
            s for s in all_sources
            if s.last_fetched_at is None or s.last_fetched_at < cutoff
        ]
        skipped = len(all_sources) - len(sources)
        if not sources:
            log.debug(
                "Юзер %s: нет источников к опросу (всего %d, throttle-skip %d).",
                owner_id, len(all_sources), skipped,
            )
            return 0

        log.info(
            "Юзер %s: источников %d, к опросу %d (пропущено %d, интервал=%dмин).",
            owner_id, len(all_sources), len(sources), skipped, interval_minutes,
        )

        created = 0
        for source in sources:
            collector = self._collectors.get(source.type)
            if collector is None:
                log.info("Нет коллектора для типа %s — пропуск", source.type.value)
                continue

            try:
                # collector.collect(source, owner_id) — унифицированная сигнатура.
                items = await collector.collect(source, owner_id)
            except Exception as exc:  # noqa: BLE001
                log.exception("Коллектор %s упал (user=%s): %s",
                              source.type.value, owner_id, exc)
                continue

            for item in items:
                try:
                    post_id = await self._posts_service.process_collected_item(
                        owner_id, item, moderation_kb_factory=self._moderation_kb_factory,
                    )
                    if post_id is not None:
                        created += 1
                except Exception as exc:  # noqa: BLE001
                    log.exception("Пайплайн упал на элементе (user=%s) %s: %s",
                                  owner_id, item.source_url, exc)

            # Помечаем источник как опрошенный.
            async with self._session_factory() as session:
                repo = SourcesRepository(session)
                await repo.mark_fetched(owner_id, source.id)
                await session.commit()

        log.info("Юзер %s: создано черновиков %d", owner_id, created)
        return created
