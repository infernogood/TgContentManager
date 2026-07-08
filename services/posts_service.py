"""
services/posts_service.py
=========================
Оркестрация пайплайна одного собранного CollectedItem ДЛЯ КОНКРЕТНОГО ЮЗЕРА.

Multi-user изменения:
    * Все методы принимают owner_id (пользователь-владелец источника).
    * LLM/Settings вызовы с user_id=owner_id.
    * Posts создаётся с owner_id.
    * Карточка отправляется в chat_id = telegram_id юзера (берётся из Users).
    * target_channel_id читается из НАСТРОЕК юзера (свой на каждого).
"""
from __future__ import annotations

import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, InlineKeyboardMarkup

from db.database import SessionFactory
from db.models import PostStatus
from db.repositories import PostsRepository, SettingsRepository, SourcesRepository, UsersRepository
from media.downloader import classify_media, cleanup, compress_image, download
from scraper.base import CollectedItem
from services.llm_service import AnalysisResult, LLMService
from services.settings_service import SettingsService

log = logging.getLogger(__name__)


class PostsService:
    """Связывает коллекторы, LLM и Telegram-бота в единый пайплайн (per-user)."""

    def __init__(
        self,
        bot: Bot,
        settings: SettingsService,
        llm: LLMService,
        session_factory: SessionFactory,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.llm = llm
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    #  Главный метод
    # ------------------------------------------------------------------ #
    async def process_collected_item(
        self,
        owner_id: int,
        item: CollectedItem,
        moderation_kb_factory=None,
    ) -> int | None:
        """
        Полный цикл обработки одного CollectedItem для конкретного юзера.

        Возвращает post_id созданного поста, либо None если элемент отбо́рен
        (дубликат / низкий рейтинг / ошибка / нет api_key).
        """
        # Шаг 1: дедуп (в рамках юзера).
        dedup_hash = PostsRepository.make_dedup_hash(item.source_url, item.raw_text)
        async with self._session_factory() as session:
            prepo = PostsRepository(session)
            if await prepo.exists_by_hash(owner_id, dedup_hash):
                log.debug("Дубликат user=%s, пропуск: %s", owner_id, item.source_url)
                return None

        # Шаг 2: медиа.
        media_path = await self._resolve_media(owner_id, item)

        # Шаг 2.5: пропустить если у источника включён skip_if_no_media и медиа нет.
        if media_path is None and item.source_id is not None:
            async with self._session_factory() as session:
                srepo = SourcesRepository(session)
                skip = await srepo.get_skip_if_no_media(owner_id, item.source_id)
                if skip:
                    log.debug(
                        "Пропуск (нет медиа) user=%s source=%s", owner_id, item.source_id,
                    )
                    return None

        try:
            # Шаг 3: анализ LLM.
            try:
                analysis: AnalysisResult = await self.llm.analyze(owner_id, item.raw_text)
            except RuntimeError as exc:
                log.warning("LLM-анализ пропущен (user=%s): %s", owner_id, exc)
                return None

            # Шаг 4: порог рейтинга.
            threshold = await self.settings.get_int(owner_id, "min_rating_threshold", 6)
            if analysis.rating == 0 or analysis.rating < threshold:
                log.info(
                    "Отсев по рейтингу %d < %d (user=%s) для %s",
                    analysis.rating, threshold, owner_id, item.source_url,
                )
                return None

            # Шаг 5: создаём DRAFT с owner_id.
            media_type = item.media_type_hint if item.has_media else "text"
            async with self._session_factory() as session:
                prepo = PostsRepository(session)
                post = await prepo.create(
                    owner_id=owner_id,
                    source_id=item.source_id,
                    source_url=item.source_url,
                    raw_text=item.raw_text,
                    translated_text=analysis.summary,
                    rating=analysis.rating,
                    dedup_hash=dedup_hash,
                    media_type=media_type,
                    status=PostStatus.DRAFT,
                )
                await session.commit()
                post_id = post.id

            # Шаг 6: отправляем админу (т.е. самому юзеру-владельцу).
            await self._send_post_to_user(
                owner_id=owner_id,
                post_id=post_id,
                item=item,
                analysis=analysis,
                media_path=media_path,
                moderation_kb_factory=moderation_kb_factory,
            )
            return post_id
        finally:
            # Шаг 8: cleanup — гарантированно.
            if media_path is not None:
                cleanup(media_path)

    # ------------------------------------------------------------------ #
    #  Медиа (per-user — для user_agent и пр.)
    # ------------------------------------------------------------------ #
    async def _resolve_media(self, owner_id: int, item: CollectedItem) -> Path | None:
        if item.media_paths:
            return item.media_paths[0]
        if item.media_urls:
            user_agent = await self.settings.get_str(owner_id, "reddit_user_agent", "TgContentManager/1.0")
            media_path = await download(item.media_urls[0], headers={"User-Agent": user_agent})
            if media_path is not None:
                media_path = compress_image(media_path)
            return media_path
        return None

    # ------------------------------------------------------------------ #
    #  Отправка юзеру
    # ------------------------------------------------------------------ #
    async def _send_post_to_user(
        self,
        *,
        owner_id: int,
        post_id: int,
        item: CollectedItem,
        analysis: AnalysisResult,
        media_path: Path | None,
        moderation_kb_factory,
    ) -> None:
        """Отправляет карточку юзеру-владельцу источника и сохраняет file_id."""
        # Берём telegram_id из БД.
        async with self._session_factory() as session:
            urepo = UsersRepository(session)
            user = await urepo.get(owner_id)
        if user is None:
            log.warning("Юзер %s не найден при отправке карточки #%d", owner_id, post_id)
            return

        chat_id = user.telegram_id

        if moderation_kb_factory is None:
            from bot.keyboards import post_card_kb
            moderation_kb_factory = lambda pid: post_card_kb(
                pid, status=PostStatus.DRAFT, has_prev=False, has_next=False, offset=0,
            )

        kb: InlineKeyboardMarkup = moderation_kb_factory(post_id)
        caption = self._render_caption(item, analysis, post_id, media_path)

        file_id = await self._send_one(chat_id, caption, media_path, kb)

        if file_id:
            async with self._session_factory() as session:
                prepo = PostsRepository(session)
                await prepo.set_media_file_id(owner_id, post_id, file_id)
                await session.commit()

    async def _send_one(
        self,
        chat_id: int,
        caption: str,
        media_path: Path | None,
        kb: InlineKeyboardMarkup,
    ) -> str | None:
        """Отправляет в один chat_id, возвращает file_id или None."""
        try:
            if media_path is None:
                await self.bot.send_message(chat_id, caption, reply_markup=kb)
                return None

            media_type = classify_media(media_path)
            if media_type == "photo":
                msg = await self.bot.send_photo(
                    chat_id, photo=FSInputFile(media_path), caption=caption, reply_markup=kb
                )
                return msg.photo[-1].file_id if msg.photo else None
            if media_type == "video":
                msg = await self.bot.send_video(
                    chat_id, video=FSInputFile(media_path), caption=caption, reply_markup=kb
                )
                return msg.video.file_id if msg.video else None
            if media_type == "animation":
                msg = await self.bot.send_animation(
                    chat_id, animation=FSInputFile(media_path), caption=caption, reply_markup=kb
                )
                return msg.animation.file_id if msg.animation else None
            msg = await self.bot.send_document(
                chat_id, document=FSInputFile(media_path), caption=caption, reply_markup=kb
            )
            return msg.document.file_id if msg.document else None
        except TelegramBadRequest as exc:
            log.warning("Не удалось отправить карточку в chat %s: %s", chat_id, exc.message)
            return None
        except Exception as exc:  # noqa: BLE001
            log.exception("Сбой отправки в chat %s: %s", chat_id, exc)
            return None

    # ------------------------------------------------------------------ #
    #  Капшон
    # ------------------------------------------------------------------ #
    @staticmethod
    def _render_caption(
        item: CollectedItem,
        analysis: AnalysisResult,
        post_id: int,
        media_path: Path | None = None,
    ) -> str:
        # Динамический лимит: фото/видео → 1024, только текст → 4096
        has_media = media_path is not None
        limit = 1024 if has_media else 4096

        # Фиксированная часть (заголовок + ID)
        header = f"🆕 <b>Новый черновик</b>  ·  ⭐ {analysis.rating}/10\n"
        footer = f"🆔 #{post_id}"
        overhead = len(header) + len(footer) + 20  # 20 — отступы/теги
        budget = limit - overhead

        # Приоритет 1: перевод (body)
        body = (analysis.summary or item.raw_text or "").strip()

        # Приоритет 2: сырой текст (raw_preview)
        raw_preview = item.raw_text.strip() if analysis.summary else ""

        # Умное распределение бюджета
        if len(body) >= budget:
            body = body[:budget - 10] + "…"
            raw_preview = ""
        elif raw_preview:
            remaining = budget - len(body) - 30  # 30 — заголовок секции + отступы
            if len(raw_preview) > remaining:
                raw_preview = raw_preview[:max(0, remaining)] + "…"

        # Сборка
        lines = [header, body]
        if raw_preview:
            lines.append(f"\n📄 <b>Оригинал:</b>\n<blockquote>{raw_preview}</blockquote>")
        lines.append(f"\n{footer}")
        return "\n".join(lines)
