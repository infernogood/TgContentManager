"""
bot/handlers/settings.py
========================
Раздел «⚙️ Настройки API» — FSM-редактирование AI-ключа, base URL, моделей
и параметров (target_channel_id, интервалы и пр.).

Multi-user: каждая настройка пишется в БД с owner_id текущего юзера.
Чтение текущих значений — через SettingsService с fallback на системные
дефолты (если юзер не задал своё).
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import (
    EDITABLE_SETTINGS,
    SettingKeyCB,
    cancel_kb,
    settings_menu_kb,
)
from bot.states import EditSettingSG
from db.database import SessionFactory
from db.models import Users
from db.repositories import SettingsRepository
from services.llm_service import LLMService
from services.settings_service import SettingsService

router = Router(name="settings")

SECRET_KEYS = {"ai_api_key", "github_token", "newsdata_api_key"}


def _mask(value: str) -> str:
    if not value:
        return "<i>(пусто)</i>"
    if len(value) <= 8:
        return "****"
    return f"{value[:5]}{'*' * 6}{value[-3:]}"


# --------------------------------------------------------------------------- #
#  Вход в раздел
# --------------------------------------------------------------------------- #
@router.message(F.text == "⚙️ Настройки API")
async def open_settings(
    message: Message, state: FSMContext, user: Users, settings_service: SettingsService,
) -> None:
    await state.clear()
    # Читаем значения ЧЕРЕЗ сервис, чтобы виден был fallback на системные дефолты.
    lines = ["⚙️ <b>Текущие значения настроек</b>\n"]
    for key, label in EDITABLE_SETTINGS:
        val = await settings_service.get_str(user.id, key, "")
        shown = _mask(val) if key in SECRET_KEYS else (val or "<i>(пусто)</i>")
        lines.append(f"{label}: <code>{shown}</code>")
    lines.append("\nНажми на кнопку, чтобы изменить значение.")
    await message.answer("\n".join(lines), reply_markup=settings_menu_kb())


# --------------------------------------------------------------------------- #
#  FSM-редактирование
# --------------------------------------------------------------------------- #
@router.callback_query(SettingKeyCB.filter())
async def choose_setting(
    callback: CallbackQuery, callback_data: SettingKeyCB, state: FSMContext,
) -> None:
    valid_keys = {k for k, _ in EDITABLE_SETTINGS}
    if callback_data.key not in valid_keys:
        await callback.answer("Неизвестная настройка.", show_alert=True)
        return

    label = dict(EDITABLE_SETTINGS)[callback_data.key]
    is_secret_hint = (
        "\n\n🔒 Введи новое значение. Оно хранится в БД и не показывается в чате целиком."
        if callback_data.key in SECRET_KEYS
        else ""
    )
    if callback_data.key == "ai_base_url":
        is_secret_hint = (
            "\n\n🌐 Например:\n"
            "• Zhipu: <code>https://open.bigmodel.cn/api/paas/v4/</code>\n"
            "• OpenAI: <code>https://api.openai.com/v1</code>\n"
            "• Ollama: <code>http://localhost:11434/v1</code>"
        )

    await state.set_state(EditSettingSG.waiting_value)
    await state.update_data(key=callback_data.key, label=label)
    await callback.message.answer(
        f"Изменяем: <b>{label}</b>{is_secret_hint}\n\nПришли новое значение:",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(EditSettingSG.waiting_value, F.text)
async def save_setting(
    message: Message,
    state: FSMContext,
    user: Users,
    settings_service: SettingsService,
    llm_service: LLMService,
) -> None:
    """Записываем новое значение С owner_id=user.id и инвалидируем кэш.

    Для AI-настроек (api_key/base_url) дополнительно сбрасываем кэш
    LLM-клиента этого юзера — иначе старый ключ/URL будут жить до рестарта.
    """
    data = await state.get_data()
    key = data["key"]
    label = data.get("label", key)
    value = message.text.strip()

    # Валидация числовых полей.
    if key in {"collector_interval_minutes", "min_rating_threshold"}:
        if not value.isdigit():
            await message.answer("Ожидалось целое число. Попробуй ещё раз.")
            return
        if key == "min_rating_threshold" and not (1 <= int(value) <= 10):
            await message.answer("Значение должно быть в диапазоне 1-10.")
            return
        if key == "collector_interval_minutes" and int(value) < 1:
            await message.answer("Интервал не может быть меньше 1 минуты.")
            return

    async with SessionFactory() as session:
        repo = SettingsRepository(session)
        await repo.set(user.id, key, value, description=label)
        await session.commit()

    # Сбрасываем кэш именно этого юзера.
    settings_service.invalidate(user.id)
    # Если сменили api_key/base_url — пересоздать LLM-клиента юзера.
    if key in {"ai_api_key", "ai_base_url"}:
        llm_service.invalidate_client(user.id)

    await state.clear()
    shown = _mask(value) if key in SECRET_KEYS else value
    await message.answer(
        f"✅ Сохранено.\n<b>{label}</b> = <code>{shown}</code>",
        reply_markup=settings_menu_kb(),
    )


@router.message(EditSettingSG.waiting_value, F.text == "🔙 Отмена")
async def cancel_edit_setting(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Редактирование отменено.", reply_markup=settings_menu_kb())
