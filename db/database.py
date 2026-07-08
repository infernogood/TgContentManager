"""
db/database.py
==============
Инициализация async-engine, фабрика сессий и создание схемы БД.

Под капотом: SQLAlchemy 2.0 (async) + aiosqlite (async-драйвер SQLite).
Один engine на процесс. Сессии создаются на каждую логическую операцию
через `async with get_session() as session: ...`.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import CFG, DEFAULT_SETTINGS, ensure_runtime_dirs
from db.models import Base, Settings
from db.repositories import SettingsRepository

# --------------------------------------------------------------------------- #
#  Engine и фабрика сессий
# --------------------------------------------------------------------------- #
# check_same_thread=False обязателен для SQLite + async: обращения идут из
# разных тасок (aiogram handlers, APScheduler jobs) одного event-loop.
# echo=False на проде; для отладки SQL поставить True.
engine = create_async_engine(
    CFG.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)

# Фабрика: expire_on_commit=False — после commit'а объекты остаются годными
# к использованию вне сессии (важно: мы возвращаем ORM-объекты в handlers).
SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-style dependency. Удобно использовать и в handlers:
        async with get_session() as session:
            ...
    Возвращает AsyncSession и гарантированно закрывает её.
    """
    async with SessionFactory() as session:
        yield session


# --------------------------------------------------------------------------- #
#  Bootstrap: создание таблиц + сидинг дефолтных настроек
# --------------------------------------------------------------------------- #
async def init_db() -> None:
    """
    Первичная инициализация БД.

    1. Создаёт служебные директории (data/, tmp/, sessions/).
    2. Создаёт все таблицы из метаданных Base (if not exists).
    3. Сидает DEFAULT_SETTINGS с owner_id=NULL (системные дефолты) —
       только недостающие ключи. Идемпотентно.
    4. Заводит записи супер-админов из CFG.admin_ids (status=ACTIVE,
       is_super_admin=True). Это гарантирует, что первый /start от
       супер-админа сразу пустит его в систему.
    """
    ensure_runtime_dirs()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with SessionFactory() as session:
        # Системные дефолты (owner_id=None).
        srepo = SettingsRepository(session)
        for key, value in DEFAULT_SETTINGS.items():
            existing = await srepo.get(None, key)
            if existing is None:
                await srepo.set(None, key, value)

        # --- Миграция: обновляем старый промпт collector на новый ---
        old_prompt = await srepo.get(None, "system_prompt_collector")
        new_prompt = DEFAULT_SETTINGS.get("system_prompt_collector", "")
        if old_prompt and "ПРАВИЛА ОТВЕТА" not in old_prompt and new_prompt:
            await srepo.set(
                None, "system_prompt_collector", new_prompt,
                description="Системный промпт сборщика",
            )
            logging.getLogger("db.database").info(
                "Миграция: system_prompt_collector обновлён до новой версии"
            )

        await session.commit()

    # Супер-админы из .env.
    from db.repositories import UsersRepository
    from db.models import UserStatus
    if CFG.admin_ids:
        async with SessionFactory() as session:
            urepo = UsersRepository(session)
            for tg_id in CFG.admin_ids:
                await urepo.upsert_from_telegram(
                    telegram_id=tg_id, is_super_admin=True,
                )
            await session.commit()


async def dispose_db() -> None:
    """Корректно закрывает пул соединений. Вызывать при остановке приложения."""
    await engine.dispose()
