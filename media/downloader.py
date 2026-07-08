"""
media/downloader.py
===================
Скачивание медиа по URL (HTTP/HTTPS) во временную директорию.

Используется:
    * rss_collector'ом — для картинок из Reddit-фидов.
    * newsdata_collector'ом — для image_url статей.
    * github_collector'ом — для preview-картинок релизов (опционально).

НЕ используется для Telegram — там медиа качается через Telethon сразу
в tmp/, минуя HTTP. См. scraper/telegram_collector.py.

Контракт: функция download() возвращает ПУТЬ к файлу или None. None — если
скачивание не удалось (404, таймаут, пустой контент). В этом случае
пайплайн продолжает работу, просто пост уходит без медиа.
"""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

import aiohttp

from config import TMP_DIR

log = logging.getLogger(__name__)

# Маппинг Content-Type -> расширение файла.
# Берём только те, что реально отправим в Telegram как фото/видео/гифку.
MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
}

# Лимит на размер скачиваемого файла (20 МБ). Telegram Bot API через sendPhoto
# принимает до 10 МБ, через sendVideo по file_id — больше; для нашего сценария
# 20 МБ — разумный потолок, чтобы не забивать /tmp огромными видео.
MAX_DOWNLOAD_BYTES: int = 20 * 1024 * 1024

# Timeout на одно скачивание (подключение + чтение).
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)


def _guess_extension(content_type: str, url: str) -> str:
    """Определяем расширение: сначала по Content-Type, потом по URL."""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in MIME_TO_EXT:
        return MIME_TO_EXT[ct]
    # Фолбэк: парсим расширение из URL.
    path = url.split("?")[0].split("#")[0]
    if "." in path.rsplit("/", 1)[-1]:
        return "." + path.rsplit(".", 1)[-1].lower()
    return ".bin"


async def download(url: str, *, headers: dict[str, str] | None = None) -> Path | None:
    """
    Скачать файл по URL в TMP_DIR. Возвращает Path или None при неудаче.

    Файл называется <uuid><ext> — никаких пользовательских имён, чтобы
    исключить path-traversal и коллизии.
    """
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".bin"
    file_path = TMP_DIR / f"{uuid.uuid4().hex}{ext}"

    try:
        async with aiohttp.ClientSession(timeout=DOWNLOAD_TIMEOUT) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    log.debug("SKIP %s: HTTP %s", url, resp.status)
                    return None

                # Уточняем расширение по реальному Content-Type ответа.
                ext = _guess_extension(resp.headers.get("Content-Type", ""), url)
                file_path = file_path.with_suffix(ext)

                # Stream-запись с защитой от переполнения.
                written = 0
                with open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            f.close()
                            cleanup(file_path)
                            log.warning("SKIP %s: превышен лимит %d байт", url, MAX_DOWNLOAD_BYTES)
                            return None
                        f.write(chunk)
                log.debug("OK %s -> %s (%d bytes)", url, file_path, written)
                return file_path
    except (aiohttp.ClientError, TimeoutError) as exc:
        log.warning("Сбой скачивания %s: %s", url, exc)
        cleanup(file_path)
        return None
    except Exception as exc:  # noqa: BLE001
        log.exception("Непредвиденная ошибка при скачивании %s: %s", url, exc)
        cleanup(file_path)
        return None


def cleanup(path: Path | str | None) -> None:
    """Удаляет временный файл. Игнорирует отсутствие файла (идемпотентно).

    По ТЗ — вызывается функцией os.remove() сразу после получения file_id от
    Telegram. Дополнительно оборачиваем в try/except чтобы не ронять пайплайн.
    """
    if path is None:
        return
    try:
        os.remove(path)
    except (FileNotFoundError, OSError):
        # Уже удалено или нет прав — не критично.
        pass


def classify_media(path: Path | str) -> str:
    """По расширению файла определяет тип для Telegram-отправки.
    Возвращает 'photo' | 'video' | 'animation' | 'document'.
    """
    ext = Path(path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if ext == ".gif":
        return "animation"
    if ext in {".mp4", ".mov"}:
        return "video"
    return "document"
