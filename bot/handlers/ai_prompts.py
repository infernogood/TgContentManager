"""
bot/handlers/ai_prompts.py
==========================
Раздел «🧠 Настройки ИИ» — редактирование системных промптов.

Multi-user: промпты пишутся с owner_id=user.id. У каждого юзера свои.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import (
    EDITABLE_PROMPTS,
    PromptKeyCB,
    cancel_kb,
    prompts_menu_kb,
)
from bot.states import EditPromptSG
from db.database import SessionFactory
from db.models import Users
from db.repositories import SettingsRepository
from services.settings_service import SettingsService

router = Router(name="ai_prompts")


@router.message(F.text == "🧠 Настройки ИИ")
async def open_prompts(
    message: Message, state: FSMContext, user: Users, settings_service: SettingsService,
) -> None:
    await state.clear()
    lines = ["🧠 <b>Текущие настройки ИИ</b>\n"]
    for key, label in EDITABLE_PROMPTS:
        val = await settings_service.get_str(user.id, key, "")
        # Промпты показываем укороченным превью.
        preview = (val[:80] + "…") if len(val) > 80 else (val or "<i>(пусто)</i>")
        lines.append(f"{label}:\n<i>{preview}</i>\n")
    lines.append("Нажми кнопку ниже, чтобы изменить.")
    await message.answer("\n".join(lines), reply_markup=prompts_menu_kb())


@router.callback_query(PromptKeyCB.filter())
async def choose_prompt(
    callback: CallbackQuery, callback_data: PromptKeyCB, state: FSMContext,
) -> None:
    valid_keys = {k for k, _ in EDITABLE_PROMPTS}
    if callback_data.key not in valid_keys:
        await callback.answer("Неизвестный промпт.", show_alert=True)
        return

    label = dict(EDITABLE_PROMPTS)[callback_data.key]
    await state.set_state(EditPromptSG.waiting_value)
    await state.update_data(key=callback_data.key, label=label)
    await callback.message.answer(
        f"Изменяем: <b>{label}</b>\n\nПришли новый текст промпта (можно в несколько строк).",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(EditPromptSG.waiting_value, F.text)
async def save_prompt(
    message: Message, state: FSMContext, user: Users, settings_service: SettingsService,
) -> None:
    data = await state.get_data()
    key = data["key"]
    label = data.get("label", key)
    value = message.text.strip()

    if not value:
        await message.answer("Пустой ввод не допустим. Попробуй ещё раз.")
        return

    async with SessionFactory() as session:
        repo = SettingsRepository(session)
        await repo.set(user.id, key, value, description=label)
        await session.commit()

    settings_service.invalidate(user.id)

    await state.clear()
    await message.answer(
        f"✅ Сохранено.\n<b>{label}</b> обновлён.",
        reply_markup=prompts_menu_kb(),
    )


@router.message(EditPromptSG.waiting_value, F.text == "🔙 Отмена")
async def cancel_edit_prompt(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Редактирование отменено.", reply_markup=prompts_menu_kb())
