"""
bot/handlers/start.py
=====================
Точка входа: /start (для всех), /help, /cancel и back_to_menu (для ACTIVE).

ОСОБЕННОСТЬ: router НЕ имеет глобального фильтра — /start обрабатывается
для любого статуса (pending/blocked/active). Остальные команды в этом
router'е проверяют статус внутри handler'а.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards import main_menu_kb
from db.models import UserStatus, Users

router = Router(name="start")

HELP_TEXT = (
    "🤖 <b>TgContentManager</b>\n\n"
    "Я собираю контент из подключённых источников (Telegram, RSS, GitHub, News), "
    "перевожу/оцениваю через AI и присылаю тебе карточки-черновики.\n\n"
    "<b>Разделы меню:</b>\n"
    "📊 <b>База контента</b> — модерация черновиков, публикация в канал.\n"
    "📡 <b>Источники</b> — управление TG/RSS/API-источниками.\n"
    "⚙️ <b>Настройки API</b> — AI key, base URL, модель, ID канала, интервалы.\n"
    "🧠 <b>Настройки ИИ</b> — системные промпты.\n\n"
    "В любом пошаговом сценарии жми <code>/cancel</code>."
)

PENDING_TEXT = (
    "🕒 <b>Заявка отправлена</b>\n\n"
    "Твой запрос отправлен супер-администратору. После одобрения ты получишь уведомление."
)

BLOCKED_TEXT = "🚫 <b>Доступ заблокирован.</b> Свяжись с супер-админом, если это ошибка."


def _is_active(user: Users | None) -> bool:
    return user is not None and user.status == UserStatus.ACTIVE


# --------------------------------------------------------------------------- #
#  /start — для всех (включая pending/blocked)
# --------------------------------------------------------------------------- #
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, user: Users | None = None) -> None:
    await state.clear()

    if user is None:
        await message.answer("Внутренняя ошибка: профиль не найден. Попробуй позже.")
        return

    if user.status == UserStatus.PENDING:
        await message.answer(PENDING_TEXT)
        return
    if user.status == UserStatus.BLOCKED:
        await message.answer(BLOCKED_TEXT)
        return

    await message.answer(
        f"Привет, <b>{message.from_user.full_name}</b> 👋\n\n{HELP_TEXT}",
        reply_markup=main_menu_kb(is_super_admin=user.is_super_admin),
    )


# --------------------------------------------------------------------------- #
#  /help, /cancel, "🔙 Отмена" — только ACTIVE
# --------------------------------------------------------------------------- #
@router.message(Command("help"))
async def cmd_help(message: Message, user: Users | None = None) -> None:
    if not _is_active(user):
        return
    await message.answer(HELP_TEXT)


@router.message(Command("cancel"))
@router.message(F.text == "🔙 Отмена")
async def cmd_cancel(message: Message, state: FSMContext, user: Users | None = None) -> None:
    """Глобальный выход из FSM. Молча игнорим для не-active юзеров."""
    if not _is_active(user):
        return
    await state.clear()
    await message.answer(
        "Действие отменено.",
        reply_markup=main_menu_kb(is_super_admin=bool(user and user.is_super_admin)),
    )


@router.callback_query(F.data == "back_to_menu")
async def cb_back_to_menu(
    callback: CallbackQuery, state: FSMContext, user: Users | None = None,
) -> None:
    if not _is_active(user):
        await callback.answer("Доступ закрыт.", show_alert=True)
        return
    await state.clear()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=main_menu_kb(is_super_admin=user.is_super_admin),
    )
    try:
        await callback.message.delete()
    except Exception:  # noqa: BLE001
        pass
    await callback.answer()
