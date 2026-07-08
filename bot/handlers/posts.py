"""
bot/handlers/posts.py
=====================
Раздел «📊 База контента» — модерация черновиков.

Multi-user: все запросы фильтруются по user.id (owner_id). Юзер видит и
может публиковать ТОЛЬКО свои посты.
"""
from __future__ import annotations

import re
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from bot.keyboards import (
    PostCB,
    PostNavCB,
    back_to_menu_kb,
    post_card_kb,
    posts_submenu_kb,
)
from db.database import SessionFactory
from db.models import PostStatus, Users
from db.repositories import PostsRepository, SettingsRepository

# router БЕЗ фильтра — он подключается в родительский active-router,
# где уже навешан IsActiveUser.
router = Router(name="posts")


# Telegram поддерживает только ограниченный набор HTML-тегов.
# Все остальные нужно удалить.
ALLOWED_TAGS = {"b", "i", "u", "s", "a", "code", "pre", "blockquote"}


def _sanitize_html(text: str) -> str:
    """Удаляет все HTML-теги, кроме разрешённых Telegram."""
    # Шаг 1: удаляем HTML-комментарии (включая многострочные)
    text = re.sub(r'<!--[\s\S]*?-->', '', text)

    # Шаг 2: удаляем самозакрывающиеся теги (например, <br />)
    text = re.sub(r'<\s*/?\s*(br|hr|img|input)\s*/?\s*>', '', text, flags=re.IGNORECASE)

    # Шаг 3: для <a> тегов — удаляем пустые или некорректные href
    def clean_a_href(match: re.Match) -> str:
        attrs = match.group(1)
        # Ищем href атрибут
        href_match = re.search(r'href\s*=\s*["\']([^"\']*)["\']', attrs, re.IGNORECASE)
        if not href_match:
            # href отсутствует — удаляем открывающий тег, оставляем содержимое
            return ''
        href_value = href_match.group(1).strip()
        if not href_value or href_value == '""' or href_value == "''":
            # href пустой — удаляем открывающий тег, оставляем содержимое
            return ''
        # href валиден — оставляем тег, но удаляем остальные атрибуты (Telegram разрешает только href)
        return f'<a href="{href_value}">'

    # Заменяем <a ...> на <a href="..."> (или удаляем, если href некорректен)
    text = re.sub(r'<a([^>]*)>', clean_a_href, text, flags=re.IGNORECASE)

    # Шаг 4: удаляем оставшиеся <a> без href (если они есть)
    text = re.sub(r'<a\s*>', '', text, flags=re.IGNORECASE)

    # Шаг 5: удаляем оставшиеся закрывающие теги </a> без пар (которые остались после удаления <a>)
    text = re.sub(r'</a\s*>', '', text, flags=re.IGNORECASE)

    # Шаг 6: удаляем открывающие и закрывающие теги, кроме разрешённых
    def remove_tag(match: re.Match) -> str:
        tag_name = match.group(1).lower()
        if tag_name in ALLOWED_TAGS:
            return match.group(0)  # оставляем тег как есть
        return ''  # удаляем тег, но содержимое остаётся

    # Удаляем открывающие теги <...>, кроме разрешённых
    text = re.sub(r'<\s*(\w+)(?:\s+[^>]*)?\s*>', remove_tag, text)

    # Удаляем закрывающие теги </...>, кроме разрешённых
    text = re.sub(r'<\s*/\s*(\w+)\s*>', remove_tag, text)

    return text

TOP_LIMIT = 10
RAW_PREVIEW_LEN = 500


# --------------------------------------------------------------------------- #
#  Хелперы рендера
# --------------------------------------------------------------------------- #
def render_post_caption(
    *, post_id: int, rating: int, source_label: str,
    translated: str, raw: str, source_url: str, created_at: datetime,
) -> str:
    raw_preview = _sanitize_html((raw or "").strip())
    if len(raw_preview) > RAW_PREVIEW_LEN:
        raw_preview = raw_preview[:RAW_PREVIEW_LEN] + "…"

    sanitized_translated = _sanitize_html(translated.strip() if translated else "")
    body = sanitized_translated if sanitized_translated else "<i>(перевод отсутствует)</i>"

    lines = [
        f"⭐ <b>Рейтинг:</b> {rating}/10",
        f"📡 <b>Источник:</b> {source_label}",
        f"📅 <b>Собран:</b> {created_at:%Y-%m-%d %H:%M}",
        "",
        body,
    ]
    if raw_preview:
        lines += ["", "<b>📄 Оригинал:</b>", f"<blockquote>{raw_preview}</blockquote>"]
    if source_url:
        lines += ["", f"🔗 <a href=\"{source_url}\">открыть источник</a>"]
    lines.append(f"\n🆔 <code>#{post_id}</code>")
    return "\n".join(lines)


async def _send_post_card(
    bot: Bot, chat_id: int, *, post, status: PostStatus, offset: int, total: int,
) -> int:
    source_label = (
        f"{post.source.title or '(без названия)'} [{post.source.type.value}]"
        if post.source else "(источник удалён)"
    )
    caption = render_post_caption(
        post_id=post.id, rating=post.rating, source_label=source_label,
        translated=post.translated_text, raw=post.raw_text,
        source_url=post.source_url, created_at=post.created_at,
    )
    kb = post_card_kb(
        post.id, status=status, offset=offset,
        has_prev=offset > 0, has_next=offset < total - 1,
    )

    file_id = post.media_file_id
    if file_id and post.media_type == "photo":
        sent = await bot.send_photo(chat_id, photo=file_id, caption=caption, reply_markup=kb)
    elif file_id and post.media_type == "video":
        sent = await bot.send_video(chat_id, video=file_id, caption=caption, reply_markup=kb)
    elif file_id and post.media_type == "animation":
        sent = await bot.send_animation(chat_id, animation=file_id, caption=caption, reply_markup=kb)
    else:
        sent = await bot.send_message(chat_id, text=caption, reply_markup=kb)
    return sent.message_id


async def _try_delete(message: Message) -> None:
    try:
        await message.delete()
    except (TelegramBadRequest, Exception):  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
#  Вход в раздел
# --------------------------------------------------------------------------- #
@router.message(F.text == "📊 База контента")
async def open_posts_menu(message: Message, user: Users) -> None:
    async with SessionFactory() as session:
        prepo = PostsRepository(session)
        drafts = await prepo.count_by_status(user.id, PostStatus.DRAFT)
        archived = await prepo.count_by_status(user.id, PostStatus.ARCHIVED)

    text = (
        "📊 <b>База контента</b>\n\n"
        f"🆕 Черновиков на модерацию: <b>{drafts}</b>\n"
        f"📦 В архиве: <b>{archived}</b>\n\n"
        "Выбери, что открыть:"
    )
    await message.answer(text, reply_markup=posts_submenu_kb())


# --------------------------------------------------------------------------- #
#  Пагинация
# --------------------------------------------------------------------------- #
@router.callback_query(PostNavCB.filter())
async def paginate_posts(
    callback: CallbackQuery, callback_data: PostNavCB, bot: Bot, user: Users,
) -> None:
    status = PostStatus(callback_data.status)

    if callback_data.direction == "next":
        offset = callback_data.offset + 1
    elif callback_data.direction == "prev":
        offset = max(callback_data.offset - 1, 0)
    else:
        offset = 0

    async with SessionFactory() as session:
        prepo = PostsRepository(session)
        total = await prepo.count_by_status(user.id, status)
        if total == 0:
            await callback.message.answer(
                "Здесь пусто 🤷‍♂️ Контента с таким статусом нет.",
                reply_markup=back_to_menu_kb(),
            )
            await _try_delete(callback.message)
            await callback.answer()
            return

        offset = min(offset, total - 1)
        posts_page = await prepo.list_by_status(user.id, status, limit=1, offset=offset)
        if not posts_page:
            await callback.answer("Посты не найдены.", show_alert=True)
            return
        post = posts_page[0]

    await _send_post_card(
        bot, callback.from_user.id,
        post=post, status=status, offset=offset, total=total,
    )
    await _try_delete(callback.message)
    await callback.answer()


# --------------------------------------------------------------------------- #
#  Действия модерации
# --------------------------------------------------------------------------- #
@router.callback_query(PostCB.filter(F.action == "approve"))
async def approve_post(
    callback: CallbackQuery, callback_data: PostCB, bot: Bot, user: Users,
) -> None:
    """✅ Опубликовать: переотправка в КАНАЛ ЮЗЕРА через file_id."""
    async with SessionFactory() as session:
        # target_channel_id берём ИЗ НАСТРОЕК ЮЗЕРА (свой у каждого).
        srepo = SettingsRepository(session)
        target = (await srepo.get(user.id, "target_channel_id") or "").strip()

        if not target:
            await callback.answer(
                "⚠️ Не задан ID твоего канала.\nЗайди в ⚙️ Настройки API.",
                show_alert=True,
            )
            return

        prepo = PostsRepository(session)
        post = await prepo.get(user.id, callback_data.post_id)
        if post is None:
            await callback.answer("Пост уже удалён или не твой.", show_alert=True)
            return

        try:
            if post.media_file_id and post.media_type == "photo":
                await bot.send_photo(target, photo=post.media_file_id, caption=post.translated_text)
            elif post.media_file_id and post.media_type == "video":
                await bot.send_video(target, video=post.media_file_id, caption=post.translated_text)
            elif post.media_file_id and post.media_type == "animation":
                await bot.send_animation(target, animation=post.media_file_id, caption=post.translated_text)
            else:
                await bot.send_message(target, text=post.translated_text)
        except TelegramBadRequest as exc:
            await callback.answer(f"Ошибка публикации: {exc.message[:120]}", show_alert=True)
            return

        await prepo.update_status(user.id, post.id, PostStatus.APPROVED, set_published_at=True)
        await session.commit()

    await callback.answer("✅ Опубликовано в канал!")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Готово. Пост опубликован.", reply_markup=back_to_menu_kb())


@router.callback_query(PostCB.filter(F.action == "reject"))
async def reject_post(callback: CallbackQuery, callback_data: PostCB, user: Users) -> None:
    async with SessionFactory() as session:
        prepo = PostsRepository(session)
        await prepo.update_status(user.id, callback_data.post_id, PostStatus.REJECTED)
        await session.commit()
    await callback.answer("🗑 В мусор.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Пост помечен как мусор.", reply_markup=back_to_menu_kb())


@router.callback_query(PostCB.filter(F.action == "archive"))
async def archive_post(callback: CallbackQuery, callback_data: PostCB, user: Users) -> None:
    async with SessionFactory() as session:
        prepo = PostsRepository(session)
        await prepo.update_status(user.id, callback_data.post_id, PostStatus.ARCHIVED)
        await session.commit()
    await callback.answer("📦 В архив.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Пост перемещён в архив.", reply_markup=back_to_menu_kb())


@router.callback_query(PostCB.filter(F.action == "redraft"))
async def redraft_post(callback: CallbackQuery, callback_data: PostCB, user: Users) -> None:
    async with SessionFactory() as session:
        prepo = PostsRepository(session)
        await prepo.update_status(user.id, callback_data.post_id, PostStatus.DRAFT)
        await session.commit()
    await callback.answer("♻️ В черновиках.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("Пост снова в очереди модерации.", reply_markup=back_to_menu_kb())


# --------------------------------------------------------------------------- #
#  Топ контента
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "list_top")
async def list_top(callback: CallbackQuery, user: Users) -> None:
    async with SessionFactory() as session:
        prepo = PostsRepository(session)
        top = await prepo.list_best(user.id, status=None, limit=TOP_LIMIT)

    if not top:
        await callback.message.answer(
            "Пока нет оценённого контента.", reply_markup=back_to_menu_kb(),
        )
        await callback.answer()
        return

    lines = ["⭐ <b>Топ контента по рейтингу ИИ</b>\n"]
    for i, p in enumerate(top, start=1):
        title = (p.translated_text or p.raw_text or "").strip().split("\n")[0][:80]
        lines.append(f"{i}. <b>{p.rating}/10</b> — {title}  <code>#{p.id}</code>")
    lines.append("\nОткрой «Черновики», чтобы отмодерировать их.")
    await callback.message.answer("\n".join(lines), reply_markup=back_to_menu_kb())
    await _try_delete(callback.message)
    await callback.answer()
