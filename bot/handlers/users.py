"""
bot/handlers/users.py
=====================
Раздел «👥 Пользователи» — только для супер-админов.

Возможности:
    * Просмотр заявок (PENDING) с кнопками одобрить/отклонить.
    * Просмотр всех пользователей со статусом и кнопками заблокировать/разблокировать.
    * При одобрении — бот автоматически шлёт юзеру уведомление в ЛС.
    * Супер-админа нельзя заблокировать (защита в репозитории).

Все запросы используют aiogram DI: user (текущий супер-админ) приходит из
UserMiddleware. Дополнительно router фильтруется IsSuperAdmin.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from bot.filters import IsSuperAdmin
from bot.keyboards import (
    UserCB,
    back_to_menu_kb,
    main_menu_kb,
    user_actions_kb,
    users_menu_kb,
)
from db.database import SessionFactory
from db.models import UserStatus, Users
from db.repositories import UsersRepository

router = Router(name="users")
# Только супер-админы могут пользоваться всем этим router'ом.
router.message.filter(IsSuperAdmin())
router.callback_query.filter(IsSuperAdmin())


def _format_user_line(u: Users) -> str:
    """Текстовое представление юзера в листинге."""
    badge = {"pending": "🕒", "active": "✅", "blocked": "🚫"}.get(u.status.value, "❓")
    crown = " 👑" if u.is_super_admin else ""
    username = f"@{u.username}" if u.username else "(без username)"
    return (
        f"{badge} <b>{u.full_name}</b>{crown}\n"
        f"{username} · ID <code>{u.telegram_id}</code> · БД #{u.id}\n"
        f"Статус: <i>{u.status.value}</i>"
    )


# --------------------------------------------------------------------------- #
#  Вход в раздел
# --------------------------------------------------------------------------- #
@router.message(F.text == "👥 Пользователи")
async def open_users_menu(message: Message) -> None:
    # Считаем бейджи.
    async with SessionFactory() as session:
        urepo = UsersRepository(session)
        pending = len(await urepo.list_pending())
        total = await urepo.count()

    text = (
        "👥 <b>Управление пользователями</b>\n\n"
        f"🕒 Заявок на рассмотрение: <b>{pending}</b>\n"
        f"Всего пользователей: <b>{total}</b>"
    )
    await message.answer(text, reply_markup=users_menu_kb())


# --------------------------------------------------------------------------- #
#  Листинги
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "users_pending")
async def list_pending(callback: CallbackQuery) -> None:
    async with SessionFactory() as session:
        urepo = UsersRepository(session)
        items = await urepo.list_pending()

    if not items:
        await callback.message.answer(
            "🎉 Нет ожидающих заявок.",
            reply_markup=users_menu_kb(),
        )
        await callback.answer()
        return

    await callback.message.answer(f"🕒 <b>Заявки ({len(items)})</b>")
    for u in items:
        await callback.message.answer(
            _format_user_line(u),
            reply_markup=user_actions_kb(u.id, u.status.value, u.is_super_admin),
        )
    await callback.message.answer("Конец списка.", reply_markup=users_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "users_all")
async def list_all(callback: CallbackQuery) -> None:
    async with SessionFactory() as session:
        urepo = UsersRepository(session)
        items = await urepo.list_all()

    if not items:
        await callback.message.answer("Пользователей нет.", reply_markup=users_menu_kb())
        await callback.answer()
        return

    await callback.message.answer(f"📋 <b>Все пользователи ({len(items)})</b>")
    for u in items:
        await callback.message.answer(
            _format_user_line(u),
            reply_markup=user_actions_kb(u.id, u.status.value, u.is_super_admin),
        )
    await callback.message.answer("Конец списка.", reply_markup=users_menu_kb())
    await callback.answer()


# --------------------------------------------------------------------------- #
#  Действия
# --------------------------------------------------------------------------- #
@router.callback_query(UserCB.filter(F.action == "approve"))
async def approve_user(callback: CallbackQuery, callback_data: UserCB, bot: Bot) -> None:
    async with SessionFactory() as session:
        urepo = UsersRepository(session)
        ok = await urepo.set_status(callback_data.user_id, UserStatus.ACTIVE)
        if ok:
            target = await urepo.get(callback_data.user_id)
        await session.commit()

    if not ok:
        await callback.answer("Не удалось (возможно, это супер-админ).", show_alert=True)
        return

    # Уведомляем юзера, что его приняли.
    if target is not None:
        try:
            await bot.send_message(
                target.telegram_id,
                "🎉 <b>Доступ одобрен!</b>\n\nТы можешь пользоваться ботом. "
                "Жми /start, чтобы открыть меню.",
                reply_markup=main_menu_kb(is_super_admin=target.is_super_admin),
            )
        except TelegramBadRequest as exc:
            # Юзер мог заблокировать бота — не критично.
            await callback.message.answer(
                f"⚠️ Не удалось уведомить юзера (он мог заблокировать бота): {exc.message}"
            )

    await callback.answer("✅ Одобрен")
    await callback.message.edit_text(
        f"{_format_user_line(target)}\n\n→ <b>ОДОБРЕН</b>"
    )


@router.callback_query(UserCB.filter(F.action == "block"))
async def block_user(callback: CallbackQuery, callback_data: UserCB, bot: Bot) -> None:
    async with SessionFactory() as session:
        urepo = UsersRepository(session)
        ok = await urepo.set_status(callback_data.user_id, UserStatus.BLOCKED)
        if ok:
            target = await urepo.get(callback_data.user_id)
        await session.commit()

    if not ok:
        await callback.answer("Нельзя заблокировать супер-админа.", show_alert=True)
        return

    if target is not None:
        try:
            await bot.send_message(
                target.telegram_id,
                "🚫 <b>Доступ к боту заблокирован супер-админом.</b>",
            )
        except TelegramBadRequest:
            pass

    await callback.answer("🚫 Заблокирован")
    await callback.message.edit_text(
        f"{_format_user_line(target)}\n\n→ <b>ЗАБЛОКИРОВАН</b>"
    )


@router.callback_query(UserCB.filter(F.action == "unblock"))
async def unblock_user(callback: CallbackQuery, callback_data: UserCB, bot: Bot) -> None:
    async with SessionFactory() as session:
        urepo = UsersRepository(session)
        ok = await urepo.set_status(callback_data.user_id, UserStatus.ACTIVE)
        if ok:
            target = await urepo.get(callback_data.user_id)
        await session.commit()

    if not ok:
        await callback.answer("Юзер не найден.", show_alert=True)
        return

    if target is not None:
        try:
            await bot.send_message(
                target.telegram_id,
                "✅ <b>Доступ восстановлен.</b> Жми /start.",
            )
        except TelegramBadRequest:
            pass

    await callback.answer("✅ Разблокирован")
    await callback.message.edit_text(
        f"{_format_user_line(target)}\n\n→ <b>РАЗБЛОКИРОВАН</b>"
    )
