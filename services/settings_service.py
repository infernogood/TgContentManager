"""
services/settings_service.py
============================
Per-user кэширующий доступ к таблице Settings с fallback на системные дефолты.

Multi-user модель:
    * Системные дефолты (owner_id=NULL) — общие для всех, сидируются в init_db.
    * Персональные значения (owner_id=<id>) — у каждого юзера свои, имеют
      ПРИОРИТЕТ над дефолтами. Если юзер не задал ключ — берётся дефолт.

Кэш:
    * self._system_cache: dict[str, str]      — системные дефолты (owner_id=NULL).
    * self._user_cache: dict[int, dict[str, str]] — per-user override'ы.
    * get(user_id, key) = user_cache.get(key) или system_cache.get(key).
    * invalidate(user_id=None) — сброс кэша юзера (или всего при None).

Все методы теперь принимают user_id первым аргументом — это контракт.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

from sqlalchemy.ext.asyncio import async_sessionmaker

from db.repositories import SettingsRepository

log = logging.getLogger(__name__)


class SettingsService:
    """Per-user кэш настроек с TTL и fallback на системные дефолты."""

    DEFAULT_TTL_SECONDS: int = 60

    def __init__(
        self,
        session_factory: async_sessionmaker,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._ttl = ttl_seconds

        # Кэш системных дефолтов (owner_id=None).
        self._system_cache: dict[str, str] = {}
        self._system_loaded_at: float = 0.0

        # Per-user кэш: user_id -> {key: value}.
        self._user_cache: dict[int, dict[str, str]] = {}
        self._user_loaded_at: dict[int, float] = {}

    # ------------------------------------------------------------------ #
    #  Загрузка кэша
    # ------------------------------------------------------------------ #
    async def _ensure_system_fresh(self) -> None:
        if self._system_cache and (time.time() - self._system_loaded_at) < self._ttl:
            return
        await self.reload_system()

    async def _ensure_user_fresh(self, user_id: int) -> None:
        if user_id in self._user_cache and (time.time() - self._user_loaded_at.get(user_id, 0)) < self._ttl:
            return
        await self.reload_user(user_id)

    async def reload_system(self) -> None:
        """Перечитывает системные дефолты."""
        try:
            async with self._session_factory() as session:
                repo = SettingsRepository(session)
                items = await repo.list_for_owner(None)
            self._system_cache = {item.key: item.value for item in items}
            self._system_loaded_at = time.time()
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось перечитать системные настройки: %s", exc)

    async def reload_user(self, user_id: int) -> None:
        """Перечитывает персональные настройки юзера."""
        try:
            async with self._session_factory() as session:
                repo = SettingsRepository(session)
                items = await repo.list_for_owner(user_id)
            self._user_cache[user_id] = {item.key: item.value for item in items}
            self._user_loaded_at[user_id] = time.time()
        except Exception as exc:  # noqa: BLE001
            log.exception("Не удалось перечитать настройки юзера %s: %s", user_id, exc)

    async def reload(self) -> None:
        """Перечитывает весь кэш: системные + всех известных юзеров."""
        await self.reload_system()
        for uid in list(self._user_cache.keys()):
            await self.reload_user(uid)

    def invalidate(self, user_id: int | None = None) -> None:
        """
        Сброс кэша.
            user_id=None  — сбросить ВСЁ (системное + всех юзеров).
            user_id=<id>  — сбросить только указанного юзера (и системное,
                            т.к. часто системный ключ меняется в тех же handler'ах).
        """
        if user_id is None:
            self._system_cache.clear()
            self._system_loaded_at = 0.0
            self._user_cache.clear()
            self._user_loaded_at.clear()
        else:
            self._user_cache.pop(user_id, None)
            self._user_loaded_at.pop(user_id, None)
            # Системный кэш инвалидируем тоже: иногда админ правит системные
            # дефолты через UI (как супер-админ), и юзеры должны увидеть сразу.
            self._system_loaded_at = 0.0

    # ------------------------------------------------------------------ #
    #  Публичный API — типизированные геттеры (per-user)
    # ------------------------------------------------------------------ #
    async def get(self, user_id: int, key: str, default: str | None = None) -> str | None:
        """Get с fallback: персональное -> системное -> default."""
        await self._ensure_system_fresh()
        await self._ensure_user_fresh(user_id)

        user_val = self._user_cache.get(user_id, {}).get(key)
        if user_val is not None:
            return user_val

        sys_val = self._system_cache.get(key)
        if sys_val is not None:
            return sys_val

        return default

    async def get_str(self, user_id: int, key: str, default: str = "") -> str:
        value = await self.get(user_id, key)
        return value if value is not None else default

    async def get_int(self, user_id: int, key: str, default: int = 0) -> int:
        value = await self.get(user_id, key)
        if value is None or not value.strip():
            return default
        try:
            return int(value)
        except ValueError:
            log.warning("Settings[user=%s][%s]=%r не int — fallback to %d",
                        user_id, key, value, default)
            return default

    async def get_bool(self, user_id: int, key: str, default: bool = False) -> bool:
        value = (await self.get(user_id, key) or "").strip().lower()
        if not value:
            return default
        return value in {"1", "true", "yes", "on", "да"}

    async def as_dict(self, user_id: int, keys: Iterable[str]) -> dict[str, str]:
        """Массовый get с fallback. Удобно вызвать один раз в начале пайплайна."""
        await self._ensure_system_fresh()
        await self._ensure_user_fresh(user_id)
        result: dict[str, str] = {}
        for k in keys:
            result[k] = (
                self._user_cache.get(user_id, {}).get(k)
                or self._system_cache.get(k)
                or ""
            )
        return result
