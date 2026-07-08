"""
main.py
=======
Точка входа приложения. Связывает в ОДНОМ asyncio event-loop:

    1. aiogram Bot + Dispatcher (Telegram Admin Panel).
    2. Telethon client (тихое чтение TG-каналов-доноров).
    3. APScheduler AsyncIOScheduler (cron-запуск коллектора).
    4. Сервисный слой: SettingsService / LLMService / PostsService.

ВАЖНО про scheduler:
    * APScheduler тикает каждые SCHEDULER_TICK_MINUTES (по умолчанию 1) минуту.
    * CollectorManager САМ решает, какие источники «дозрели» (по их
      last_fetched_at vs setting collector_interval_minutes). Это позволяет
      админу менять интервал через чат без reschedule-логики.

ВАЖНО про settings_service:
    * Прокидывается в handlers через aiogram DI (kwargs в start_polling).
    * Handler'ы настроек/промптов вызывают settings_service.invalidate()
      после записи -> кэш сбрасывается -> новое значение подхватывается
      мгновенно на следующем тике коллектора.

ДЕПЛОЙ: см. README.md. Кратко:
    python -m venv venv && source venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env  # заполнить первичные ключи
    python main.py        # первый запуск (для интерактивной auth Telethon)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from bot.bot import create_bot, create_dispatcher
from bot.keyboards import post_card_kb
from config import CFG, ensure_runtime_dirs
from db.database import SessionFactory, dispose_db, init_db
from db.models import PostStatus
from scraper.manager import CollectorManager
from services.llm_service import LLMService
from services.posts_service import PostsService
from services.settings_service import SettingsService

# Telethon — опциональная зависимость. Если не установлен или не
# сконфигурирован, приложение работает, но TG-источники пропускаются.
try:
    from telethon import TelegramClient
    HAS_TELETHON = True
except ImportError:
    HAS_TELETHON = False
    TelegramClient = None  # type: ignore[assignment, misc]

if TYPE_CHECKING:
    from telethon import TelegramClient as _TgClient  # только для тайпхинтов

# --------------------------------------------------------------------------- #
#  Конфигурация запуска
# --------------------------------------------------------------------------- #
# Частота тиков шедулера. Меньше = точнее throttle, но больше проверок БД.
# 1 минута — разумный баланс.
SCHEDULER_TICK_MINUTES: int = 1

# Имя APScheduler-задачи (для идемпотентности и graceful shutdown).
COLLECTOR_JOB_ID: str = "collector_cycle"


def configure_logging() -> None:
    """Унифицированная настройка логирования.

    Уровень берётся из env LOG_LEVEL (INFO по умолчанию).
    APScheduler в отдельном логгере приглушаем до WARNING — иначе он
    спамит каждую секунду.
    """
    import os

    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Приглушаем шумные логгеры.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.INFO)
    logging.getLogger("aiogram.event_loop").setLevel(logging.WARNING)


log = logging.getLogger("main")


# --------------------------------------------------------------------------- #
#  Telethon
# --------------------------------------------------------------------------- #
def is_telethon_configured() -> bool:
    """Telethon включаем только если api_id/api_hash заданы в .env."""
    return bool(
        HAS_TELETHON
        and CFG.telethon_api_id
        and CFG.telethon_api_hash
    )


async def start_telethon() -> "TelegramClient | None":
    """
    Создаёт и стартует Telethon-клиент.

    Если session-файл уже существует (создан при первом интерактивном
    запуске) — start() не требует интерактива.
    Если файла НЕТ — start() попытается спросить phone+code через stdin.
    На сервере без TTY это зависнет; для первичной авторизации запустите
    main.py локально/через SSH с TTY один раз, чтобы создать session-файл.

    При любой ошибке возвращаем None — приложение продолжит работу
    без поддержки TG-источников (RSS/GitHub/NewsData остаются активны).
    """
    if not is_telethon_configured():
        log.warning(
            "Telethon не сконфигурирован (TELETHON_API_ID/API_HASH пусты). "
            "TG-источники будут пропускаться."
        )
        return None

    if not HAS_TELETHON:
        log.warning("Библиотека telethon не установлена — TG-коллектор недоступен.")
        return None

    try:
        client = TelegramClient(
            CFG.telethon_session,        # путь к session-файлу
            CFG.telethon_api_id,
            CFG.telethon_api_hash,
        )
        await client.start()
        me = await client.get_me()
        log.info(
            "Telethon запущен от имени @%s (id=%s). TG-коллектор активен.",
            getattr(me, "username", None), getattr(me, "id", None),
        )
        return client
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Не удалось запустить Telethon: %s. "
            "TG-источники отключены. Для первичной авторизации запусти main.py "
            "локально с TTY, чтобы создать session-файл.",
            exc,
        )
        return None


# --------------------------------------------------------------------------- #
#  Клавиатура модерации для пайплайна
# --------------------------------------------------------------------------- #
def make_moderation_kb(post_id: int):
    """Фабрика inline-клавиатуры для карточки черновика.

    Используется PostsService'ом при отправке поста админу. has_prev/has_next
    False, offset=0 — это «одиночная» карточка (не в режиме листания).
    """
    return post_card_kb(
        post_id,
        status=PostStatus.DRAFT,
        has_prev=False,
        has_next=False,
        offset=0,
    )


# --------------------------------------------------------------------------- #
#  Главный цикл
# --------------------------------------------------------------------------- #
async def main() -> None:
    """Точка входа. Поднимает всё и держит event loop до SIGINT/SIGTERM."""
    configure_logging()
    log.info("=== TgContentManager startup ===")

    # 1. Служебные директории + схема БД.
    ensure_runtime_dirs()
    await init_db()
    log.info("БД инициализирована: %s", CFG.database_url)

    # 2. Сервисный слой.
    settings_svc = SettingsService(SessionFactory)
    await settings_svc.reload()  # прогреваем кэш
    llm_svc = LLMService(settings_svc)

    # 3. aiogram-бот.
    bot = create_bot()
    # ВАЖНО: передаём session_factory — UserMiddleware её использует для
    # подгрузки юзера на каждый Update.
    dp = create_dispatcher(SessionFactory)

    # 4. Telethon (опционально).
    telethon_client = await start_telethon()

    # 5. PostsService + CollectorManager.
    posts_svc = PostsService(
        bot=bot,
        settings=settings_svc,
        llm=llm_svc,
        session_factory=SessionFactory,
    )
    manager = CollectorManager(
        session_factory=SessionFactory,
        settings=settings_svc,
        posts_service=posts_svc,
        telethon_client=telethon_client,
        moderation_kb_factory=make_moderation_kb,
        bot=bot,
    )

    # 6. APScheduler.
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        manager.run_once,
        trigger="interval",
        minutes=SCHEDULER_TICK_MINUTES,
        id=COLLECTOR_JOB_ID,
        # max_instances=1 — не запускать новый такт, если прошлый ещё бежит
        # (LLM-вызовы могут длиться дольше минуты).
        max_instances=1,
        # coalesce=True — если было несколько пропущенных тиков, выполнить один.
        coalesce=True,
        # next_run_time — первый прогон ЧЕРЕЗ минуту, а не сразу при старте.
        # Сразу не запускаем, чтобы не мешать первичному сбору логов startup'а.
    )
    scheduler.start()
    log.info(
        "APScheduler запущен. Тик каждые %d мин. Collector решает per-source throttle.",
        SCHEDULER_TICK_MINUTES,
    )

    # 7. Старт polling. Передаём settings_service в kwargs -> aiogram DI
    # разнесёт его по handler'ам, у которых в сигнатуре есть такой параметр.
    me = await bot.get_me()
    log.info("Бот @%s (id=%s) готов к работе.", me.username, me.id)

    try:
        # Печать инфо о готовности.
        enabled = ", ".join(t.value for t in manager._collectors.keys()) or "(пусто)"
        log.info("Активные коллекторы: %s", enabled)
        await dp.start_polling(
            bot,
            settings_service=settings_svc,
        )
    finally:
        # 8. Graceful shutdown в обратном порядке.
        log.info("=== shutdown: останавливаю компоненты ===")
        try:
            scheduler.shutdown(wait=False)
            log.info("APScheduler остановлен.")
        except Exception as exc:  # noqa: BLE001
            log.warning("Сбой остановки scheduler: %s", exc)

        if telethon_client is not None:
            try:
                await telethon_client.disconnect()
                log.info("Telethon отключён.")
            except Exception as exc:  # noqa: BLE001
                log.warning("Сбой отключения Telethon: %s", exc)

        try:
            await bot.session.close()
            log.info("Bot session закрыта.")
        except Exception as exc:  # noqa: BLE001
            log.warning("Сбой закрытия bot session: %s", exc)

        await dispose_db()
        log.info("БД соединения закрыты. Bye.")


# --------------------------------------------------------------------------- #
#  Энтрипоинт
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        # Ctrl+C / systemd stop — штатный выход, без traceback.
        log.info("Получен сигнал остановки.")
    except RuntimeError as exc:
        # Например, BOT_TOKEN пуст.
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)
