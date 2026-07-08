"""
bot/keyboards.py
================
Все Reply/Inline клавиатуры бота в одном модуле.

CallbackData-фактори (PostCB, SourceCB, ...) — идиоматичный способ aiogram 3.x
типизировать payload колбэков. Вместо строк вида "post:approve:42" мы пакуем
объект, и в handler'е анрапим его уже с готовым post_id типа int.
"""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db.models import PostStatus, SourceType

# =========================================================================== #
#  Reply-клавиатуры (главное меню)
# =========================================================================== #
def main_menu_kb(is_super_admin: bool = False) -> ReplyKeyboardMarkup:
    """
    Постоянное меню внизу чата.
    Кнопка "👥 Пользователи" показывается ТОЛЬКО супер-админам.
    """
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text="📊 База контента")],
        [
            KeyboardButton(text="📡 Источники"),
            KeyboardButton(text="⚙️ Настройки API"),
        ],
        [KeyboardButton(text="🧠 Настройки ИИ")],
    ]
    if is_super_admin:
        rows.append([KeyboardButton(text="👥 Пользователи")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    """Меню из одной кнопки 'Отмена' — показывается во время FSM-ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔙 Отмена")]],
        resize_keyboard=True,
    )


# =========================================================================== #
#  CallbackData-фактори (типизированные payload'ы колбэков)
# =========================================================================== #
class PostCB(CallbackData, prefix="post"):
    """Действие над конкретным постом: опубликовать/архив/удалить/показать."""

    action: str     # approve | archive | reject | view | redraft
    post_id: int


class PostNavCB(CallbackData, prefix="pnav"):
    """Пагинация по списку постов."""

    direction: str  # next | prev | init
    status: str     # draft | approved | rejected | archived
    offset: int     # текущее смещение


class SourceCB(CallbackData, prefix="src"):
    """Действие над источником: toggle/delete."""

    action: str     # toggle | del
    source_id: int


class SourceAddCB(CallbackData, prefix="srcadd"):
    """Старт FSM добавления источника заданного типа."""

    source_type: str  # tg | rss | github | newsdata


class SettingKeyCB(CallbackData, prefix="set"):
    """Открытие конкретной настройки для редактирования (через FSM)."""

    key: str


class PromptKeyCB(CallbackData, prefix="prm"):
    """Открытие конкретного промпта для редактирования (через FSM)."""

    key: str


class UserCB(CallbackData, prefix="usr"):
    """Действие супер-админа над заявкой/учёткой юзера."""

    action: str   # approve | block | unblock | promote
    user_id: int


# =========================================================================== #
#  Inline-клавиатуры — модерируемые карточки постов
# =========================================================================== #
def post_card_kb(
    post_id: int,
    *,
    status: PostStatus = PostStatus.DRAFT,
    offset: int = 0,
    has_prev: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    """
    Карточка поста.

    Row 1: ✅ Опубликовать | 📦 В архив | 🗑 Удалить
    Row 2: пагинация (только в режиме листания)
    Row 3: 🔙 В меню

    Пагинация показывается ТОЛЬКО когда есть куда листать, чтобы не плодить
    мёртвые кнопки. На карточке уже опубликованного поста кнопки модерации
    прячутся (остаются только просмотр + архив).
    """
    kb = InlineKeyboardBuilder()

    if status == PostStatus.DRAFT:
        # Полный набор действий модерации.
        kb.row(
            InlineKeyboardButton(
                text="✅ Опубликовать",
                callback_data=PostCB(action="approve", post_id=post_id).pack(),
            ),
            InlineKeyboardButton(
                text="📦 В архив",
                callback_data=PostCB(action="archive", post_id=post_id).pack(),
            ),
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=PostCB(action="reject", post_id=post_id).pack(),
            ),
        )
    elif status == PostStatus.ARCHIVED:
        # Архив можно вернуть в черновики.
        kb.row(
            InlineKeyboardButton(
                text="♻️ В черновики",
                callback_data=PostCB(action="redraft", post_id=post_id).pack(),
            ),
            InlineKeyboardButton(
                text="🗑 Удалить",
                callback_data=PostCB(action="reject", post_id=post_id).pack(),
            ),
        )

    # Пагинация.
    nav_buttons: list[InlineKeyboardButton] = []
    if has_prev:
        nav_buttons.append(
            InlineKeyboardButton(
                text="◀️ Пред.",
                callback_data=PostNavCB(
                    direction="prev", status=status.value, offset=offset
                ).pack(),
            )
        )
    if has_next:
        nav_buttons.append(
            InlineKeyboardButton(
                text="След. ▶️",
                callback_data=PostNavCB(
                    direction="next", status=status.value, offset=offset
                ).pack(),
            )
        )
    if nav_buttons:
        kb.row(*nav_buttons)

    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))
    return kb.as_markup()


# =========================================================================== #
#  Меню источников
# =========================================================================== #
def sources_menu_kb() -> InlineKeyboardMarkup:
    """Корневое меню раздела «📡 Источники»."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📋 Список источников", callback_data="src_list"))
    kb.row(
        InlineKeyboardButton(text="➕ TG-канал", callback_data=SourceAddCB("tg").pack()),
        InlineKeyboardButton(text="➕ RSS", callback_data=SourceAddCB("rss").pack()),
    )
    kb.row(
        InlineKeyboardButton(text="➕ GitHub", callback_data=SourceAddCB("github").pack()),
        InlineKeyboardButton(text="➕ NewsData", callback_data=SourceAddCB("newsdata").pack()),
    )
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))
    return kb.as_markup()


def source_actions_kb(source_id: int, enabled: bool) -> InlineKeyboardMarkup:
    """Кнопки под конкретным источником в листинге."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'выкл' if enabled else 'вкл'}",
                    callback_data=SourceCB(action="toggle", source_id=source_id).pack(),
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить",
                    callback_data=SourceCB(action="del", source_id=source_id).pack(),
                ),
            ],
        ]
    )


def source_type_kb() -> InlineKeyboardMarkup:
    """Выбор типа источника (альтернатива отдельным кнопкам в главном меню)."""
    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="Telegram", callback_data=SourceAddCB("tg").pack()),
        InlineKeyboardButton(text="RSS", callback_data=SourceAddCB("rss").pack()),
    )
    kb.row(
        InlineKeyboardButton(text="GitHub", callback_data=SourceAddCB("github").pack()),
        InlineKeyboardButton(text="NewsData", callback_data=SourceAddCB("newsdata").pack()),
    )
    kb.row(InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel_fsm"))
    return kb.as_markup()


# =========================================================================== #
#  Меню настроек API
# =========================================================================== #
# Реестр редактируемых настроек: key -> человекочитаемое имя.
# ИЗМЕНЕНО под провайдер-агностику: ai_api_key + ai_base_url + ai_model_*.
EDITABLE_SETTINGS: list[tuple[str, str]] = [
    ("ai_api_key", "🔑 AI API Key"),
    ("ai_base_url", "🌐 AI Base URL (провайдер)"),
    ("ai_model_collector", "🧠 Модель «Сборщик»"),
    ("ai_model_writer", "🧠 Модель «Писатель»"),
    ("github_token", "🐙 GitHub Token"),
    ("newsdata_api_key", "📰 NewsData.io API Key"),
    ("target_channel_id", "📍 ID целевого канала"),
    ("collector_interval_minutes", "⏱ Интервал парсинга (мин)"),
    ("min_rating_threshold", "🎯 Мин. порог рейтинга (1-10)"),
    ("reddit_user_agent", "🤖 User-Agent для Reddit"),
]


def settings_menu_kb() -> InlineKeyboardMarkup:
    """Меню выбора настройки для редактирования."""
    kb = InlineKeyboardBuilder()
    for key, label in EDITABLE_SETTINGS:
        kb.row(InlineKeyboardButton(text=label, callback_data=SettingKeyCB(key=key).pack()))
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))
    return kb.as_markup()


# =========================================================================== #
#  Меню промптов
# =========================================================================== #
EDITABLE_PROMPTS: list[tuple[str, str]] = [
    ("system_prompt_collector", "🔍 Промпт «Сборщик»"),
    ("system_prompt_writer", "✍️ Промпт «Писатель»"),
]


def prompts_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, label in EDITABLE_PROMPTS:
        kb.row(InlineKeyboardButton(text=label, callback_data=PromptKeyCB(key=key).pack()))
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))
    return kb.as_markup()


# =========================================================================== #
#  Прочее
# =========================================================================== #
def posts_submenu_kb() -> InlineKeyboardMarkup:
    """Подменю раздела «📊 База контента»: выбрать какой список показать."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🆕 Черновики", callback_data=PostNavCB(direction="init", status="draft", offset=0).pack()))
    kb.row(InlineKeyboardButton(text="⭐ Топ контента", callback_data="list_top"))
    kb.row(
        InlineKeyboardButton(text="📦 Архив", callback_data=PostNavCB(direction="init", status="archived", offset=0).pack()),
    )
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))
    return kb.as_markup()


def back_to_menu_kb() -> InlineKeyboardMarkup:
    """Универсальная inline-кнопка «в главное меню»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")]]
    )


# =========================================================================== #
#  Меню управления юзерами (только супер-админ)
# =========================================================================== #
def users_menu_kb() -> InlineKeyboardMarkup:
    """Корневое меню раздела «👥 Пользователи»."""
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="🕒 Заявки на доступ", callback_data="users_pending"))
    kb.row(InlineKeyboardButton(text="📋 Все пользователи", callback_data="users_all"))
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu"))
    return kb.as_markup()


def user_actions_kb(target_user_id: int, status: str, is_super: bool = False) -> InlineKeyboardMarkup:
    """Кнопки действий над конкретным юзером.

    status — текущее состояние из UserStatus.value.
    Не даём блокировать/понижать супер-админа (is_super=True).
    """
    kb = InlineKeyboardBuilder()
    if is_super:
        # Супер-админа нельзя трогать.
        kb.row(InlineKeyboardButton(text="👑 Супер-админ (не редактируется)", callback_data="noop"))
        return kb.as_markup()

    if status == "pending":
        kb.row(
            InlineKeyboardButton(
                text="✅ Одобрить",
                callback_data=UserCB(action="approve", user_id=target_user_id).pack(),
            ),
            InlineKeyboardButton(
                text="🚫 Отклонить",
                callback_data=UserCB(action="block", user_id=target_user_id).pack(),
            ),
        )
    elif status == "active":
        kb.row(
            InlineKeyboardButton(
                text="🚫 Заблокировать",
                callback_data=UserCB(action="block", user_id=target_user_id).pack(),
            ),
        )
    elif status == "blocked":
        kb.row(
            InlineKeyboardButton(
                text="✅ Разблокировать",
                callback_data=UserCB(action="unblock", user_id=target_user_id).pack(),
            ),
        )
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="users_all"))
    return kb.as_markup()
