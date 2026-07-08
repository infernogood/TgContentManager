"""
media/downloader.py
===================
Скачивание медиа по URL (HTTP/HTTPS) во временную директорию.
Сжатие изображений перед отправкой в Telegram.

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
from PIL import Image

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

# Расширения изображений, которые можно сжимать.
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".tif"} 


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


def compress_image(
    path: Path, *,
    max_side: int = 1080,
    quality: int = 85,
) -> Path:
    """
    Сжать изображение: ресайз (длинная сторона ≤ max_side) + конвертация в JPEG.

    Цель — уменьшить вес 2–4 МБ → 150–300 КБ, после чего система будет
    рассчитывать лимит бюджета caption (1024/4096).

    Алгоритм:
    1. Проверяем расширение — если не изображение, возвращаем path без изменений.
    2. Открываем через Pillow, определяем оригинальный размер.
    3. Если длинная сторона > max_side — ресайзим с сохранением пропорций.
    4. Конвертируем в RGB (на случай RGBA/палитры).
    5. Сохраняем как JPEG с quality=85, перезаписывая оригинал.

    Args:
        path: Путь к скачанному файлу.
        max_side: Максимальная длина длинной стороны в пикселях.
        quality: Качество JPEG (0–100). 85 — оптимум качество/размер.

    Returns:
        Path к сжатому файлу (тот же path, перезаписан).
        Если файл не изображение или ошибка — возвращаем path без изменений.
    """
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return path

    try:
        with Image.open(path) as img:
            width, height = img.size

            # Если изображение уже меньше лимита — только пересохраняем как JPEG
            # для единообразия формата и дополнительного сжатия.
            longest = max(width, height)
            if longest > max_side:
                ratio = max_side / longest
                new_width = round(width * ratio)
                new_height = round(height * ratio)
                img = img.resize((new_width, new_height), Image.LANCZOS)
                log.debug(
                    "Ресайз %s: %dx%d → %dx%d",
                    path.name, width, height, new_width, new_height,
                )

            # Конвертация в RGB (Pillow не сохраняет RGBA как JPEG).
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")

            # Перезаписываем оригинал — старый файл удаляется.
            # Меняем расширение на .jpg для единообразия.
            jpeg_path = path.with_suffix(".jpg")
            img.save(jpeg_path, "JPEG", quality=quality, optimize=True)

            # Если путь изменился (был .png → .jpg), удаляем оригинал.
            if jpeg_path != path:
                cleanup(path)

            size_kb = jpeg_path.stat().st_size / 1024
            log.debug(
                "Сжатие %s: %.0f КБ, quality=%d, max_side=%d",
                jpeg_path.name, size_kb, quality, max_side,
            )
            return jpeg_path

    except Exception:
        log.exception("Ошибка сжатия %s — пропускаем", path)
        return path


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
