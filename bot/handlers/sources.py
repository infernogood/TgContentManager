"""
bot/handlers/sources.py
======================
Раздел «📡 Источники» — управление источниками юзера через FSM.

Multi-user: источники создаются/читаются ТОЛЬКО для текущего user.id.
"""
from __future__ import annotations

import json

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import (
    SourceAddCB,
    SourceCB,
    cancel_kb,
    source_actions_kb,
    sources_menu_kb,
)
from bot.states import AddSourceSG
from db.database import SessionFactory
from db.models import SourceType, Users
from db.repositories import SourcesRepository

router = Router(name="sources")


SOURCE_TYPE_HINTS: dict[SourceType, str] = {
    SourceType.TG: "Пришли username канала без @.\nНапример: <code>durov</code>",
    SourceType.RSS: "Пришли полный URL RSS-фида.\nНапример: <code>https://www.reddit.com/r/Python.rss</code>",
    SourceType.GITHUB: (
        "Режим репозитория: пришли <code>owner/repo</code> (напр. <code>tiangolo/fastapi</code>).\n"
        "Режим поиска: пришли <code>-</code> (минус), чтобы искать по тегам на следующем шаге."
    ),
    SourceType.NEWSDATA: "Пришли поисковый запрос для NewsData.io.\nНапример: <code>AI OR \"machine learning\"</code>",
}


# --------------------------------------------------------------------------- #
#  Вход в раздел + список
# --------------------------------------------------------------------------- #
@router.message(F.text == "📡 Источники")
async def open_sources(message: Message, state: FSMContext, user: Users) -> None:
    await state.clear()
    await message.answer("📡 <b>Источники контента</b>", reply_markup=sources_menu_kb())


@router.callback_query(F.data == "src_list")
async def list_sources(callback: CallbackQuery, user: Users) -> None:
    async with SessionFactory() as session:
        repo = SourcesRepository(session)
        items = await repo.list_all(user.id)

    if not items:
        await callback.message.answer(
            "Пока нет ни одного источника.\nДобавь первым кнопкой ➕ ниже.",
            reply_markup=sources_menu_kb(),
        )
        await callback.answer()
        return

    await callback.message.answer("📋 <b>Все источники:</b>")
    for src in items:
        flag = "✅" if src.enabled else "⛔"
        title = f" — {src.title}" if src.title else ""
        text = (
            f"{flag} <b>{src.type.value.upper()}</b>{title}\n"
            f"<code>{src.identifier}</code>\n"
            f"🆔 {src.id}"
        )
        await callback.message.answer(text, reply_markup=source_actions_kb(src.id, src.enabled))

    await callback.message.answer("Действия — под каждым источником.", reply_markup=sources_menu_kb())
    await callback.answer()


@router.callback_query(SourceCB.filter(F.action == "toggle"))
async def toggle_source(callback: CallbackQuery, callback_data: SourceCB, user: Users) -> None:
    async with SessionFactory() as session:
        repo = SourcesRepository(session)
        new_state = await repo.toggle(user.id, callback_data.source_id)
        await session.commit()

    if new_state is None:
        await callback.answer("Источник не найден или не твой.", show_alert=True)
        return

    await callback.answer("Включён ✅" if new_state else "Выключен ⛔")
    await list_sources(callback, user)


@router.callback_query(SourceCB.filter(F.action == "del"))
async def delete_source(callback: CallbackQuery, callback_data: SourceCB, user: Users) -> None:
    async with SessionFactory() as session:
        repo = SourcesRepository(session)
        ok = await repo.delete(user.id, callback_data.source_id)
        await session.commit()

    if not ok:
        await callback.answer("Источник не найден или не твой.", show_alert=True)
        return

    await callback.answer("🗑 Удалён.")
    await list_sources(callback, user)


# --------------------------------------------------------------------------- #
#  Добавление (FSM)
# --------------------------------------------------------------------------- #
@router.callback_query(SourceAddCB.filter())
async def start_add_source(
    callback: CallbackQuery, callback_data: SourceAddCB, state: FSMContext,
) -> None:
    source_type = SourceType(callback_data.source_type)
    await state.set_state(AddSourceSG.waiting_identifier)
    await state.update_data(source_type=source_type.value)
    await callback.message.answer(
        f"➕ Добавляем источник типа <b>{source_type.value.upper()}</b>.\n\n"
        f"{SOURCE_TYPE_HINTS[source_type]}",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AddSourceSG.waiting_identifier, F.text)
async def process_identifier(message: Message, state: FSMContext) -> None:
    identifier = message.text.strip()
    if not identifier:
        await message.answer("Пустой ввод. Попробуй ещё раз или жми «🔙 Отмена».")
        return
    data = await state.get_data()
    source_type = SourceType(data["source_type"])

    if source_type == SourceType.GITHUB:
        await state.update_data(repo_identifier=identifier)
        await state.set_state(AddSourceSG.waiting_topics)
        await message.answer(
            "🏷 <b>Топики для поиска (по желанию)</b>\n\n"
            "Пришли список тегов, <b>каждый с новой строки</b>:\n"
            "<code>llm</code>\n<code>python</code>\n\n"
            "Если хочешь отслеживать конкретный репозиторий (что выше) — "
            "отправь <code>-</code> (минус) для пропуска.",
            reply_markup=cancel_kb(),
        )
    else:
        await state.update_data(identifier=identifier)
        await state.set_state(AddSourceSG.waiting_title)
        await message.answer(
            "Теперь пришли <b>короткое название</b> для этого источника "
            "(напр. «r/Python»).\n\nОтправь <code>-</code>, чтобы оставить без названия.",
            reply_markup=cancel_kb(),
        )


@router.message(AddSourceSG.waiting_topics, F.text)
async def process_topics(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    topics = []
    if raw != "-":
        for line in raw.split("\n"):
            line = line.strip()
            if line:
                topics.append(line)

    await state.update_data(topics=topics)
    await state.set_state(AddSourceSG.waiting_title)
    await message.answer(
        "Теперь пришли <b>короткое название</b> для этого источника.\n\nОтправь <code>-</code>, чтобы оставить без названия.",
        reply_markup=cancel_kb(),
    )


@router.message(AddSourceSG.waiting_topics, F.text == "🔙 Отмена")
async def cancel_add_source_topics(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Добавление отменено.", reply_markup=sources_menu_kb())


@router.message(AddSourceSG.waiting_title, F.text)
async def process_title(message: Message, state: FSMContext, user: Users) -> None:
    data = await state.get_data()
    title_raw = message.text.strip()
    title = "" if title_raw == "-" else title_raw

    source_type = SourceType(data["source_type"])

    if source_type == SourceType.GITHUB:
        repo_identifier = data.get("repo_identifier", "")
        topics = data.get("topics", [])
        extra_data = {}
        identifier = repo_identifier

        if topics:
            extra_data["topics"] = topics
            if not identifier or identifier == "-":
                identifier = f"topic:{topics[0]}"  # Fallback для поиска

        extra = json.dumps(extra_data) if extra_data else None
    else:
        identifier = data.get("identifier", "")
        extra = None

    async with SessionFactory() as session:
        repo = SourcesRepository(session)
        await repo.add(
            owner_id=user.id,
            type_=source_type,
            identifier=identifier,
            title=title,
            extra=extra,
        )
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Источник <b>{source_type.value.upper()}</b> добавлен.\n"
        f"<code>{identifier}</code>",
        reply_markup=sources_menu_kb(),
    )


@router.message(AddSourceSG.waiting_identifier, F.text == "🔙 Отмена")
@router.message(AddSourceSG.waiting_title, F.text == "🔙 Отмена")
async def cancel_add_source(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Добавление отменено.", reply_markup=sources_menu_kb())
