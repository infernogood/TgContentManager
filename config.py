"""
config.py
=========
Центральный модуль конфигурации приложения.

ВАЖНО: согласно ТЗ, в коде/`.env` хранятся ТОЛЬКО первичные данные подключения.
Все остальные настройки (API-ключи Zhipu/GitHub/News, ID целевого канала,
системные промпты, интервалы) хранятся в таблице `Settings` и редактируются
через интерфейс Telegram-бота. См. db/repositories.py -> SettingsRepository.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Загружаем переменные окружения из .env (для локальной разработки).
# На проде Ubuntu переменные обычно задаются через systemd-юнит или docker env.
load_dotenv()


# --------------------------------------------------------------------------- #
#  Пути в файловой системе
# --------------------------------------------------------------------------- #
PROJECT_ROOT: Path = Path(__file__).resolve().parent

# Директория под SQLite-файл. Создаётся автоматически при старте приложения.
DATA_DIR: Path = PROJECT_ROOT / "data"

# Временная директория под скачанные медиа перед отправкой в Telegram.
# На Ubuntu это будет ./tmp/, но путь переопределяется переменной TMP_DIR,
# чтобы при необходимости указать системный /tmp.
TMP_DIR: Path = Path(os.getenv("TMP_DIR", PROJECT_ROOT / "tmp"))

# Директория под файл сессии Telethon (telethon.session).
SESSIONS_DIR: Path = PROJECT_ROOT / "sessions"


# --------------------------------------------------------------------------- #
#  Primary-настройки (читаются ТОЛЬКО из окружения — не из БД)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PrimaryConfig:
    """Настройки, которые нельзя менять из чата бота."""

    # Токен aiogram-бота (получить у @BotFather).
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))

    # Telegram-ID СУПЕР-администраторов через запятую: "123456789,987654321".
    # Супер-админы: всегда активны, видят меню "👥 Пользователи" и одобряют
    # заявки обычных юзеров. Обычные юзеры регистрируются через /start и
    # ждут одобрения. См. db/models.py -> Users.
    admin_ids: tuple[int, ...] = field(
        default_factory=lambda: tuple(
            int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
        )
    )

    # Telethon (userbot для чтения каналов-доноров).
    # Получить на https://my.telegram.org -> API development tools.
    telethon_api_id: int = field(
        default_factory=lambda: int(os.getenv("TELETHON_API_ID", "0") or 0)
    )
    telethon_api_hash: str = field(default_factory=lambda: os.getenv("TELETHON_API_HASH", ""))
    telethon_session: str = field(
        default_factory=lambda: os.getenv("TELETHON_SESSION", "sessions/telethon")
    )

    # DSN для SQLAlchemy. По умолчанию — SQLite в ./data/content.db.
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite+aiosqlite:///{(DATA_DIR / 'content.db').as_posix()}"
        )
    )

    # --- LLM-провайдер: переопределение через .env (deployment-level) ---
    # Если эти переменные заданы, они ВЫИГРЫВАЮТ над значениями в БД.
    # Удобно для закрепления провайдера/модели на уровне окружения
    # (напр., при использовании прокси/зеркала или пине конкретной модели).
    # Работает с любым OpenAI-совместимым API (Zhipu, OpenAI, Ollama, vLLM...).

    # Base URL API провайдера. Если пусто — берётся из БД
    # (по умолчанию Zhipu: https://open.bigmodel.cn/api/paas/v4/).
    ai_base_url_env: str = field(default_factory=lambda: os.getenv("AI_BASE_URL", ""))

    # Закрепить модель сборщика (иначе берётся из БД, по умолчанию glm-4-flash).
    ai_model_collector_env: str = field(
        default_factory=lambda: os.getenv("AI_MODEL_COLLECTOR", "")
    )
    # Закрепить модель писателя (иначе берётся из БД, по умолчанию glm-4).
    ai_model_writer_env: str = field(
        default_factory=lambda: os.getenv("AI_MODEL_WRITER", "")
    )


# Глобальный синглтон. Импортируется всеми модулями: `from config import CFG`.
CFG = PrimaryConfig()


# --------------------------------------------------------------------------- #
#  Дефолтные значения, которыми инициализируется таблица Settings при первом
#  запуске (init_db). Значения пустые/минимальные — реальный контент юзер
#  (или супер-админ) прописывает через чат.
#
#  Multi-user: эти дефолты сидаются с owner_id=NULL как СИСТЕМНЫЕ. Каждый
#  юзер наследует их, пока не переопределит через чат своими значениями.
# --------------------------------------------------------------------------- #
DEFAULT_SETTINGS: dict[str, str] = {
    # --- LLM-провайдер (любой OpenAI-совместимый: Zhipu, OpenAI, Ollama, vLLM...) ---
    "ai_api_key": "",
    # OpenAI-compatible base URL. Zhipu: https://open.bigmodel.cn/api/paas/v4/
    "ai_base_url": "https://open.bigmodel.cn/api/paas/v4/",
    "ai_model_collector": "glm-4-flash",   # дёшево/быстро: оценка + перевод
    "ai_model_writer": "glm-4",            # качество: написание постов

    # --- Внешние API ---
    "github_token": "",
    "newsdata_api_key": "",
    "reddit_user_agent": "TgContentManager/1.0",

    # --- Канал назначения (куда публиковать approved-посты). У каждого юзера свой. ---
    "target_channel_id": "",

    # --- Интервалы парсинга (минуты) ---
    "collector_interval_minutes": "60",

    # --- Порог отсева: посты с rating < порога не доходят до модерации ---
    "min_rating_threshold": "6",

    # --- Системные промпты LLM ---
    "system_prompt_collector": (
        "Ты — аналитик контента технических каналов.\n\n"
        "ЗАДАЧА: оцени релевантность поста для технической аудитории (1-10) "
        "и сделай краткий перевод на русский язык.\n\n"
        "ПРАВИЛА ОТВЕТА:\n"
        "1. Ответь ТОЛЬКО валидным JSON. Никакого текста до или после.\n"
        "2. Никаких markdown, эмодзи или форматирования.\n"
        '3. Формат: {"rating": 7, "summary": "перевод поста"}\n\n'
        "ПРИМЕР:\n"
        '{"rating": 8, "summary": "Вышла новая версия FastAPI с улучшенной производительностью."}'
    ),
    "system_prompt_writer": (
        "Ты — копирайтер русскоязычного технического Telegram-канала. На основе "
        "предоставленного материала напиши готовый пост: живой тон, эмодзи в меру, "
        "не больше 1500 символов. Не выдумывай фактов."
    ),
}


def ensure_runtime_dirs() -> None:
    """Создаёт служебные директории, если их ещё нет. Безопасно вызывать N раз."""
    for directory in (DATA_DIR, TMP_DIR, SESSIONS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
