"""
bot/filters.py
==============
Кастомные фильтры aiogram 3.x для multi-user системы.

Три уровня доступа:
    * IsActiveUser — пропускает только ACTIVE юзеров (основная аудитория).
    * IsSuperAdmin — только is_super_admin=True (меню "👥 Пользователи").
    * AnyRegisteredUser — любой из таблицы Users (для /start, чтобы отдать
      корректное сообщение "ожидает одобрения" вместо игнора).

Фильтры записывают ORM-объект юзера в data["user"] для последующих
handler'ов через aiogram DI: `async def handler(message, user: Users)`.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from config import CFG
from db.database import SessionFactory
from db.models import UserStatus, Users
from db.repositories import UsersRepository


# --------------------------------------------------------------------------- #
#  Middleware: подгружает/создаёт юзера на каждый Update
# --------------------------------------------------------------------------- #
class UserMiddleware(BaseMiddleware):
    """
    На каждый Update:
        1. Ищет юзера по telegram_id в БД.
        2. Если не найден И telegram_id в CFG.admin_ids — создаёт супер-админа
           (страховка; обычно init_db уже создал).
        3. Если не найден и не админ — создаёт PENDING-запись (новая заявка).
        4. Кладёт Users ORM в data["user"] (или None, если что-то странное).
        5. Открывает AsyncSession в data["session"] для использования в handler'ах.

    Все handler'ы могут принимать user: Users через DI — и фильтр IsXxx
    ниже уже работает по data["user"].
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user = getattr(event, "from_user", None)
        if tg_user is None:
            return await handler(event, data)

        async with self._session_factory() as session:
            urepo = UsersRepository(session)
            is_super = tg_user.id in CFG.admin_ids
            user = await urepo.upsert_from_telegram(
                telegram_id=tg_user.id,
                username=tg_user.username or "",
                full_name=tg_user.full_name,
                is_super_admin=is_super,
            )
            await session.commit()
            # Важно: expire_on_commit=False, поэтому объект юзера живёт вне сессии.
            data["user"] = user

        return await handler(event, data)


# --------------------------------------------------------------------------- #
#  Фильтры доступа
# --------------------------------------------------------------------------- #
class IsActiveUser(BaseFilter):
    """Пропускает только ACTIVE юзеров."""

    async def __call__(self, event: Message | CallbackQuery, **data: Any) -> bool:
        user = data.get("user")
        return user is not None and user.status == UserStatus.ACTIVE


class IsSuperAdmin(BaseFilter):
    """Пропускает только супер-админов."""

    async def __call__(self, event: Message | CallbackQuery, **data: Any) -> bool:
        user = data.get("user")
        return user is not None and user.is_super_admin
