"""
db/models.py
============
ORM-модели SQLAlchemy 2.0 (новый стиль: Mapped[...] / mapped_column).

Multi-user архитектура (SaaS):
    * Users     — пользователи бота (telegram_id, is_active, is_super_admin).
    * Settings  — KV-настройки С owner_id (NULL = системный дефолт).
    * Sources   — источники, принадлежащие конкретному юзеру.
    * Posts     — посты, принадлежащие конкретному юзеру (через owner_id).

Изоляция данных:
    Все запросы Sources/Posts фильтруются по owner_id. Settings имеет
    fallback: если у юзера нет своей записи по ключу, берётся системная
    (owner_id IS NULL). Логика fallback — в SettingsService.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# --------------------------------------------------------------------------- #
#  Базовый класс всех моделей
# --------------------------------------------------------------------------- #
class Base(DeclarativeBase):
    """Декларативный базовый класс SQLAlchemy 2.0."""


# BigInt PK, который в SQLite становится INTEGER (надо для автоинкремента).
BigIntPK = BigInteger().with_variant(Integer, "sqlite")


# --------------------------------------------------------------------------- #
#  Enum'ы
# --------------------------------------------------------------------------- #
class SourceType(str, enum.Enum):
    """Тип источника. Маппится на свой коллектор в scraper/."""

    TG = "tg"                  # Telegram-канал через Telethon
    RSS = "rss"                # RSS/Atom фид
    GITHUB = "github"          # GitHub REST API
    NEWSDATA = "newsdata"      # NewsData.io


class PostStatus(str, enum.Enum):
    """Жизненный цикл карточки контента."""

    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class UserStatus(str, enum.Enum):
    """Состояние учётки пользователя."""

    PENDING = "pending"        # зарегистрировался, ждёт одобрения супер-админом
    ACTIVE = "active"          # одобрен, может пользоваться ботом
    BLOCKED = "blocked"        # заблокирован супер-админом


# --------------------------------------------------------------------------- #
#  Users — пользователи бота
# --------------------------------------------------------------------------- #
class Users(Base):
    """
    Пользователь бота.

    * telegram_id — ID пользователя в Telegram (уникальный).
    * status      — pending/active/blocked. Новые юзеры (не из ADMIN_IDS)
                    попадают в PENDING и ждут одобрения.
    * is_super_admin — True для ID из CFG.admin_ids. Такие всегда ACTIVE
                    и видят меню управления юзерами.
    * sources / posts — связи для cascade-удаления при блокировке/удалении.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status_enum"),
        nullable=False,
        default=UserStatus.PENDING,
        index=True,
    )
    is_super_admin: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Связи.
    sources: Mapped[list[Sources]] = relationship(
        "Sources", back_populates="owner", cascade="all, delete-orphan"
    )
    posts: Mapped[list[Posts]] = relationship(
        "Posts", back_populates="owner", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Users #{self.id} tg={self.telegram_id} status={self.status.value}>"


# --------------------------------------------------------------------------- #
#  Settings — KV-хранилище (с owner_id для multi-user)
# --------------------------------------------------------------------------- #
class Settings(Base):
    """
    Универсальная таблица 'ключ-значение' с поддержкой multi-user.

    Семантика owner_id:
        NULL              — системный дефолт (сидается при init_db).
                           Доступен всем юзерам как fallback.
        <user_id>         — пользовательское переопределение.
                           Имеет приоритет над системным дефолтом.

    UniqueConstraint на (owner_id, key) защищает от дублей.
    Замечание про SQLite: NULL != NULL, поэтому для системных дефолтов
    уникальность контролируется репозиторием (см. SettingsRepository.set).
    """

    __tablename__ = "settings"
    __table_args__ = (
        UniqueConstraint("owner_id", "key", name="uq_settings_owner_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # NULL = системный дефолт; иначе — ID юзера.
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        owner = "system" if self.owner_id is None else f"user#{self.owner_id}"
        return f"<Settings [{owner}] {self.key}={self.value[:30]!r}>"


# --------------------------------------------------------------------------- #
#  Sources — источники контента
# --------------------------------------------------------------------------- #
class Sources(Base):
    """Источник контента. Принадлежит конкретному юзеру (owner_id)."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type_enum"), nullable=False, index=True
    )
    identifier: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    extra: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_fetched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped[Users] = relationship("Users", back_populates="sources")
    posts: Mapped[list[Posts]] = relationship(
        "Posts", back_populates="source", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Sources #{self.id} user={self.owner_id} {self.type.value}:{self.identifier!r}>"


# --------------------------------------------------------------------------- #
#  Posts — карточки контента
# --------------------------------------------------------------------------- #
class Posts(Base):
    """Карточка контента. Принадлежит конкретному юзеру (owner_id)."""

    __tablename__ = "posts"
    __table_args__ = (
        # Дедуп в пределах юзера: один и тот же пост не может завестись дважды
        # у одного человека. У разных юзеров — может (они независимы).
        UniqueConstraint("owner_id", "dedup_hash", name="uq_posts_owner_dedup"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )

    source_url: Mapped[str] = mapped_column(String(1024), nullable=False, default="", index=True)
    dedup_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    translated_text: Mapped[str] = mapped_column(Text, nullable=False, default="")

    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")

    rating: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[PostStatus] = mapped_column(
        Enum(PostStatus, name="post_status_enum"),
        nullable=False,
        default=PostStatus.DRAFT,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    owner: Mapped[Users] = relationship("Users", back_populates="posts")
    source: Mapped[Sources | None] = relationship("Sources", back_populates="posts")

    def __repr__(self) -> str:
        return (
            f"<Posts #{self.id} user={self.owner_id} status={self.status.value} "
            f"rating={self.rating}>"
        )
