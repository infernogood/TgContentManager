"""
scraper/telegram_collector.py
=============================
Чтение Telegram-каналов через Telethon (userbot).

ОСОБЕННОСТЬ: Telethon работает по MTProto от имени ЮЗЕР-аккаунта
(не бота!). Это позволяет ТИХО читать любые каналы без подписки
бота на канал. Настройки api_id/api_hash берём из .env (первичное
подключение), саму сессию — sessions/telethon.session.

Для каждого канала:
    * итерируем последние N сообщений (LIMIT);
    * пропускаем пустые (без текста и медиа);
    * для текста — берём как есть;
    * для фото/видео/гифки — скачиваем через Telethon в TMP/, путь кладём
      в CollectedItem.media_paths (HTTP-урлов у TG-файлов нет).

Дедупликация не делается здесь (нет публичных id) — этим занимается
pipeline по dedup_hash в БД.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from config import TMP_DIR
from db.models import SourceType, Sources
from scraper.base import BaseCollector, CollectedItem

if TYPE_CHECKING:
    from telethon import TelegramClient

log = logging.getLogger(__name__)

# Сколько последних сообщений читаем за один прогон.
DEFAULT_LIMIT = 20


class TelegramCollector(BaseCollector):
    """Читает каналы-доноры через Telethon."""

    handled_types = (SourceType.TG,)

    def __init__(self, settings, client: "TelegramClient") -> None:
        super().__init__(settings)
        self.client = client

    async def collect(self, source: Sources, owner_id: int) -> list[CollectedItem]:
        channel = source.identifier.lstrip("@")
        items: list[CollectedItem] = []

        try:
            # iter_messages — async generator. limit ограничивает глубину.
            async for msg in self.client.iter_messages(channel, limit=DEFAULT_LIMIT):
                text = (msg.text or "").strip()
                has_media = msg.media is not None

                # Пропускаем служебные сообщения без полезной нагрузки.
                if not text and not has_media:
                    continue

                media_paths: list = []
                media_type = "text"
                if has_media:
                    try:
                        # download_media возвращает путь к скачанному файлу
                        # (или None, если медиа неподдерживаемого типа).
                        path = await self.client.download_media(msg, file=TMP_DIR)
                        if path is not None:
                            media_paths.append(path)
                            media_type = self._guess_media_type(msg)
                    except Exception as exc:  # noqa: BLE001
                        # Медиа — это бонус, не критично если не скачалось.
                        log.warning("TG %s: не удалось скачать медиа msg=%s: %s",
                                    channel, msg.id, exc)

                # Публичный линк на сообщение (работает только для публичных каналов).
                source_url = f"https://t.me/{channel}/{msg.id}"

                items.append(
                    CollectedItem(
                        source_id=source.id,
                        source_url=source_url,
                        raw_text=text or "(медиа без текста)",
                        media_paths=media_paths,
                        media_type_hint=media_type,
                    )
                )
        except Exception as exc:  # noqa: BLE001
            # Например, канал не существует или нет прав — логируем и выходим.
            log.warning("TG %s: не удалось прочитать канал: %s", channel, exc)

        log.info("TG %s: собрано %d элементов", channel, len(items))
        return items

    @staticmethod
    def _guess_media_type(msg) -> str:
        """По полям Telethon-сообщения определяет тип для aiogram."""
        try:
            if msg.photo is not None:
                return "photo"
            if msg.video is not None:
                return "video"
            if msg.gif is not None or getattr(msg.document, "mime_type", "") == "video/mp4":
                return "animation"
        except AttributeError:
            pass
        return "document"
