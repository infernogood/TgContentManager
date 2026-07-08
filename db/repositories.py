"""
db/repositories.py
==================
CRUD-слой над ORM-моделями. Multi-user: все операции с Sources/Posts
параметризованы owner_id; Settings поддерживает системные дефолты (NULL)
и переопределения конкретного юзера.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import PostStatus, Posts, Settings, SourceType, Sources, UserStatus, Users


# =========================================================================== #
#  Users
# =========================================================================== #
class UsersRepository:
    """CRUD над таблицей пользователей."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> Users | None:
        result = await self.session.execute(
            select(Users).where(Users.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def get(self, user_id: int) -> Users | None:
        return await self.session.get(Users, user_id)

    async def upsert_from_telegram(
        self,
        telegram_id: int,
        username: str = "",
        full_name: str = "",
        is_super_admin: bool = False,
    ) -> Users:
        """
        Создаёт или обновляет пользователя по telegram_id.
        Возвращает ORM-объект. НЕ делает commit — вызывающий код решает.
        """
        user = await self.get_by_telegram_id(telegram_id)
        if user is None:
            # Супер-админ сразу ACTIVE, обычный — PENDING.
            status = UserStatus.ACTIVE if is_super_admin else UserStatus.PENDING
            user = Users(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name,
                status=status,
                is_super_admin=is_super_admin,
            )
            self.session.add(user)
        else:
            # Обновляем профиль, но статус не трогаем (его меняет супер-админ).
            user.username = username or user.username
            user.full_name = full_name or user.full_name
            # Повышать до супер-админа можно, понижать — нет (для безопасности).
            if is_super_admin:
                user.is_super_admin = True
                user.status = UserStatus.ACTIVE
        await self.session.flush()
        return user

    async def list_all(self) -> Sequence[Users]:
        result = await self.session.execute(select(Users).order_by(Users.created_at.desc()))
        return result.scalars().all()

    async def list_pending(self) -> Sequence[Users]:
        result = await self.session.execute(
            select(Users).where(Users.status == UserStatus.PENDING).order_by(Users.created_at.desc())
        )
        return result.scalars().all()

    async def list_active_for_collection(self) -> Sequence[Users]:
        """Юзеры, для которых надо крутить коллектор: ACTIVE и не супер-админ.
        Супер-админу тоже можно собирать — но обычно это технический аккаунт,
        поэтому по умолчанию исключаем. Если хочешь включить — убери фильтр.
        """
        result = await self.session.execute(
            select(Users).where(Users.status == UserStatus.ACTIVE).order_by(Users.id)
        )
        return result.scalars().all()

    async def set_status(self, user_id: int, status: UserStatus) -> bool:
        user = await self.get(user_id)
        if user is None:
            return False
        # Супер-админа нельзя заблокировать (защита от случайного само-бана).
        if user.is_super_admin and status == UserStatus.BLOCKED:
            return False
        user.status = status
        await self.session.flush()
        return True

    async def count(self) -> int:
        result = await self.session.execute(select(func.count(Users.id)))
        return int(result.scalar_one())


# =========================================================================== #
#  Settings: KV-хранилище (multi-user)
# =========================================================================== #
class SettingsRepository:
    """
    Работа с Settings с поддержкой owner_id.

    Семантика:
        owner_id=None  -> системный дефолт (fallback для всех юзеров).
        owner_id=<id>  -> персональное значение юзера.

    В публичных методах owner_id ВСЕГДА передаётся явно (None для system).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, owner_id: int | None, key: str) -> str | None:
        """Возвращает запись ТОЛЬКО для указанного owner_id (без fallback).
        Fallback на системный дефолт реализован в SettingsService.
        """
        result = await self.session.execute(
            select(Settings).where(Settings.owner_id.is_(owner_id), Settings.key == key)
        )
        row = result.scalar_one_or_none()
        return row.value if row else None

    async def get_or_default(self, owner_id: int | None, key: str, default: str = "") -> str:
        value = await self.get(owner_id, key)
        return value if value is not None else default

    async def set(
        self,
        owner_id: int | None,
        key: str,
        value: str,
        description: str = "",
    ) -> Settings:
        """Upsert: обновляет существующую запись (owner_id, key) или создаёт новую."""
        result = await self.session.execute(
            select(Settings).where(Settings.owner_id.is_(owner_id), Settings.key == key)
        )
        obj = result.scalar_one_or_none()

        if obj is None:
            obj = Settings(owner_id=owner_id, key=key, value=value, description=description)
            self.session.add(obj)
        else:
            obj.value = value
            if description:
                obj.description = description
        await self.session.flush()
        return obj

    async def list_for_owner(self, owner_id: int | None) -> Sequence[Settings]:
        """Все настройки конкретного owner_id."""
        result = await self.session.execute(
            select(Settings).where(Settings.owner_id.is_(owner_id)).order_by(Settings.key)
        )
        return result.scalars().all()

    async def delete(self, owner_id: int | None, key: str) -> bool:
        result = await self.session.execute(
            select(Settings).where(Settings.owner_id.is_(owner_id), Settings.key == key)
        )
        obj = result.scalar_one_or_none()
        if obj is None:
            return False
        await self.session.delete(obj)
        return True


# =========================================================================== #
#  Sources: источники (теперь с owner_id)
# =========================================================================== #
class SourcesRepository:
    """CRUD над источниками. Все методы требуют owner_id."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        owner_id: int,
        type_: SourceType,
        identifier: str,
        title: str = "",
        extra: str = "{}",
        enabled: bool = True,
    ) -> Sources:
        source = Sources(
            owner_id=owner_id,
            type=type_,
            identifier=identifier.strip(),
            title=title.strip(),
            extra=extra,
            enabled=enabled,
        )
        self.session.add(source)
        await self.session.flush()
        return source

    async def get(self, owner_id: int, source_id: int) -> Sources | None:
        """Возвращает источник ТОЛЬКО если он принадлежит owner_id."""
        result = await self.session.execute(
            select(Sources).where(Sources.id == source_id, Sources.owner_id == owner_id)
        )
        return result.scalar_one_or_none()

    async def list_all(self, owner_id: int) -> Sequence[Sources]:
        result = await self.session.execute(
            select(Sources)
            .where(Sources.owner_id == owner_id)
            .order_by(Sources.type, Sources.identifier)
        )
        return result.scalars().all()

    async def list_active(self, owner_id: int) -> Sequence[Sources]:
        result = await self.session.execute(
            select(Sources).where(Sources.owner_id == owner_id, Sources.enabled.is_(True))
        )
        return result.scalars().all()

    async def list_active_all_users(self) -> Sequence[Sources]:
        """Все активные источники ВСЕХ юзеров. Используется CollectorManager'ом."""
        result = await self.session.execute(
            select(Sources).where(Sources.enabled.is_(True))
        )
        return result.scalars().all()

    async def toggle(self, owner_id: int, source_id: int) -> bool | None:
        obj = await self.get(owner_id, source_id)
        if obj is None:
            return None
        obj.enabled = not obj.enabled
        await self.session.flush()
        return obj.enabled

    async def delete(self, owner_id: int, source_id: int) -> bool:
        obj = await self.get(owner_id, source_id)
        if obj is None:
            return False
        await self.session.delete(obj)
        return True

    async def mark_fetched(self, owner_id: int, source_id: int) -> None:
        await self.session.execute(
            update(Sources)
            .where(Sources.id == source_id, Sources.owner_id == owner_id)
            .values(last_fetched_at=datetime.utcnow())
        )


# =========================================================================== #
#  Posts: карточки контента (теперь с owner_id)
# =========================================================================== #
class PostsRepository:
    """CRUD над постами. Все методы требуют owner_id."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        owner_id: int,
        source_id: int | None,
        source_url: str,
        raw_text: str,
        translated_text: str = "",
        media_file_id: str | None = None,
        media_type: str = "text",
        rating: int = 0,
        dedup_hash: str | None = None,
        status: PostStatus = PostStatus.DRAFT,
    ) -> Posts:
        if dedup_hash is None:
            dedup_hash = self.make_dedup_hash(source_url, raw_text)
        post = Posts(
            owner_id=owner_id,
            source_id=source_id,
            source_url=source_url,
            raw_text=raw_text,
            translated_text=translated_text,
            media_file_id=media_file_id,
            media_type=media_type,
            rating=rating,
            status=status,
            dedup_hash=dedup_hash,
        )
        self.session.add(post)
        await self.session.flush()
        return post

    @staticmethod
    def make_dedup_hash(source_url: str, raw_text: str) -> str:
        payload = (source_url + "|" + raw_text).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    async def get(self, owner_id: int, post_id: int) -> Posts | None:
        from sqlalchemy.orm import selectinload

        result = await self.session.execute(
            select(Posts)
            .options(selectinload(Posts.source))
            .where(Posts.id == post_id, Posts.owner_id == owner_id)
        )
        return result.scalar_one_or_none()

    async def exists_by_hash(self, owner_id: int, dedup_hash: str) -> bool:
        result = await self.session.execute(
            select(Posts.id).where(
                Posts.owner_id == owner_id, Posts.dedup_hash == dedup_hash
            ).limit(1)
        )
        return result.first() is not None

    async def list_by_status(
        self,
        owner_id: int,
        status: PostStatus,
        limit: int = 20,
        offset: int = 0,
    ) -> Sequence[Posts]:
        from sqlalchemy.orm import selectinload

        result = await self.session.execute(
            select(Posts)
            .options(selectinload(Posts.source))
            .where(Posts.owner_id == owner_id, Posts.status == status)
            .order_by(Posts.rating.desc(), Posts.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return result.scalars().all()

    async def list_drafts(self, owner_id: int, limit: int = 10) -> Sequence[Posts]:
        return await self.list_by_status(owner_id, PostStatus.DRAFT, limit=limit)

    async def list_best(
        self,
        owner_id: int,
        status: PostStatus | None = None,
        limit: int = 10,
    ) -> Sequence[Posts]:
        from sqlalchemy.orm import selectinload

        stmt = (
            select(Posts)
            .options(selectinload(Posts.source))
            .where(Posts.owner_id == owner_id)
            .order_by(Posts.rating.desc(), Posts.created_at.desc())
        )
        if status is not None:
            stmt = stmt.where(Posts.status == status)
        stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def count_by_status(self, owner_id: int, status: PostStatus) -> int:
        result = await self.session.execute(
            select(func.count(Posts.id)).where(
                Posts.owner_id == owner_id, Posts.status == status
            )
        )
        return int(result.scalar_one())

    async def update_status(
        self,
        owner_id: int,
        post_id: int,
        status: PostStatus,
        set_published_at: bool = False,
    ) -> Posts | None:
        obj = await self.get(owner_id, post_id)
        if obj is None:
            return None
        obj.status = status
        if set_published_at or status == PostStatus.APPROVED:
            obj.published_at = datetime.utcnow()
        await self.session.flush()
        return obj

    async def set_media_file_id(self, owner_id: int, post_id: int, file_id: str) -> None:
        await self.session.execute(
            update(Posts)
            .where(Posts.id == post_id, Posts.owner_id == owner_id)
            .values(media_file_id=file_id)
        )

    async def set_translation(
        self,
        owner_id: int,
        post_id: int,
        translated_text: str,
        rating: int,
    ) -> None:
        await self.session.execute(
            update(Posts)
            .where(Posts.id == post_id, Posts.owner_id == owner_id)
            .values(translated_text=translated_text, rating=rating)
        )

    async def delete(self, owner_id: int, post_id: int) -> bool:
        obj = await self.get(owner_id, post_id)
        if obj is None:
            return False
        await self.session.delete(obj)
        return True
