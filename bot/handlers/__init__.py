"""
bot/handlers: набор Router'ов по разделам меню.

Multi-user:
    * start.router — публичный (/start для всех статусов).
    * users.router — только IsSuperAdmin.
    * Остальные (posts/sources/settings/ai_prompts) — только IsActiveUser.
"""
from __future__ import annotations

from aiogram import Router

from bot.filters import IsActiveUser
from bot.handlers import ai_prompts, posts, settings, sources, start, users


def get_main_router() -> Router:
    """Собирает все router'ы в один корневой."""
    root = Router(name="root")

    # /start обрабатывается для всех (включая pending/blocked).
    root.include_router(start.router)

    # Управление юзерами — супер-админ.
    root.include_router(users.router)

    # Остальные разделы — только активные юзеры.
    active = Router(name="active")
    active.message.filter(IsActiveUser())
    active.callback_query.filter(IsActiveUser())
    active.include_router(posts.router)
    active.include_router(sources.router)
    active.include_router(settings.router)
    active.include_router(ai_prompts.router)
    root.include_router(active)

    return root
