"""
bot/bot.py
==========
Фабрика Bot и Dispatcher aiogram 3.x (multi-user версия).

Изменения:
    * Убран глобальный фильтр IsAdmin на dp.message/callback_query.
    * Добавлен UserMiddleware: на каждый Update подгружает/создаёт юзера
      и кладёт его в data["user"] + открывает сессию.
    * Фильтры IsActiveUser/IsSuperAdmin навешиваются на конкретные router'ы.

main.py передаёт session_factory в create_dispatcher() для middleware.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.filters import UserMiddleware
from bot.handlers import get_main_router
from config import CFG

log = logging.getLogger(__name__)


def create_bot() -> Bot:
    """Создаёт Bot с HTML как дефолтным parse_mode."""
    if not CFG.bot_token:
        raise RuntimeError(
            "BOT_TOKEN не задан. Укажи его в .env или переменной окружения."
        )
    return Bot(
        token=CFG.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(session_factory) -> Dispatcher:
    """
    Собирает Dispatcher.

    session_factory нужен UserMiddleware для подгрузки юзера из БД на каждый Update.
    """
    dp = Dispatcher(storage=MemoryStorage())

    # Middleware: загружает/создаёт юзера и открывает сессию для каждого Update.
    # Внешний middleware срабатывает до router-фильтров.
    user_mw = UserMiddleware(session_factory)
    dp.message.outer_middleware(user_mw)
    dp.callback_query.outer_middleware(user_mw)

    dp.include_router(get_main_router())
    return dp


# --------------------------------------------------------------------------- #
#  Lifecycle-хуки
# --------------------------------------------------------------------------- #
async def on_startup(bot: Bot) -> None:
    """Вызывается при старте polling'а."""
    log.info("Bot startup: инициализация БД...")
    from db.database import init_db

    await init_db()
    me = await bot.get_me()
    log.info("Бот @%s (id=%s) готов к работе.", me.username, me.id)


async def on_shutdown(bot: Bot) -> None:
    """Вызывается при остановке — закрываем сессии."""
    log.info("Bot shutdown: закрываем соединения...")
    from db.database import dispose_db

    await dispose_db()
    await bot.session.close()
