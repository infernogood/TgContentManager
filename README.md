# TgContentManager

Автоматизированный SaaS-подобный сервис контент-менеджмента и кросс-постинга.
Бот-сборщик читает источники (Telegram-каналы, RSS, GitHub, NewsData.io),
прогоняет текст через **OpenAI-совместимый LLM** (Zhipu GLM, OpenAI, Ollama
и др.) для оценки и перевода, и присылает готовые карточки-черновики
администратору в Telegram на модерацию.
Веб-интерфейса нет — **вся панель управления живёт в самом Telegram-боте**.

### Multi-User SaaS

Система поддерживает **множество независимых пользователей**:
- Каждый пользователь автоматически регистрируется через `/start`.
- Супер-админ одобряет заявки через меню `👥 Пользователи`.
- Все настройки, источники и посты изолированы по `owner_id`.
- Каждый пользователь использует **свой AI-ключ, модель и base_url**.

---

## Содержание

1. [Возможности](#-возможности)
2. [Архитектура](#-архитектура)
3. [Требования](#-требования)
4. [Быстрый старт (локально)](#-быстрый-старт-локально)
5. [Получение всех ключей и токенов](#-получение-всех-ключей-и-токенов)
6. [Первичная авторизация Telethon](#-первичная-авторизация-telethon)
7. [Настройка через бот (после запуска)](#-настройка-через-бот-после-запуска)
8. [Деплой на Ubuntu (systemd)](#-деплой-на-ubuntu-systemd)
9. [Использование: разделы меню](#-использование-разделы-меню)
10. [Схема базы данных](#-схема-базы-данных)
11. [Устранение неисправностей](#-устранение-неисправностей)
12. [Расширение проекта](#-расширение-проекта)

---

## 🚀 Возможности

- **Сбор** из 4 типов источников:
  - **Telegram-каналы** — тихое чтение через Telethon (userbot), без подписки бота.
  - **RSS/Atom** — Reddit `.rss`, блоги, любые фиды.
  - **GitHub** — релизы по репозиториям (`owner/repo`).
  - **NewsData.io** — новости по поисковому запросу.
- **Анализ через LLM** (любой OpenAI-совместимый API):
  - Модель «Сборщик» оценивает релевантность (1-10) и делает перевод/выжимку.
  - Модель «Писатель» доступна для длинных постов (точка расширения).
  - Каждый пользователь использует свой API-ключ, модель и base_url.
- **Модерация в чате**: карточки с inline-кнопками ✅ Опубликовать / 📦 В архив / 🗑 Удалить.
- **Публикация через `file_id`**: носитель уже у Telegram — повторная загрузка из `/tmp/` не нужна.
- **Дедупликация** по SHA-256 от `source_url + raw_text` — повторной отправки одного поста не будет.
- **Per-source throttling**: каждый источник опрашивается не чаще, чем заданный интервал.
- **Полностью настраивается из чата**: API-ключи, промпты, модели, интервалы — без правки кода.
- **Маскировка секретов** в чате (токены не светятся при скриншотах).

---

## 🏛 Архитектура

```
                 ┌─────────────────────────────────────────┐
                 │            main.py (event loop)         │
                 │  aiogram Bot + Telethon + APScheduler   │
                 └──────────────┬──────────────────────────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  ┌──────────┐         ┌────────────────┐       ┌──────────────┐
  │ aiogram  │         │ CollectorManager│       │  Telethon    │
  │ handlers │         │ (cron, 1 мин)  │       │  (MTProto)   │
  │  (DI)    │         └───────┬────────┘       └──────┬───────┘
  └────┬─────┘                 │                       │
       │          ┌────────────┼────────────┐          │
       │          ▼            ▼            ▼          │
       │      RssColl.   GithubColl.  NewsDataColl.    │
       │          └────────────┴────────────┘          │
       │                      │                       │
       │                      ▼                       │
       │             PostsService.process()           │
       │             (download → LLM → DB → admin)    │
       │                      │                       │
       ▼                      ▼                       ▼
  ┌──────────────────────────────────────────────────────┐
  │            SQLite (SQLAlchemy 2.0 / aiosqlite)        │
  │        Settings · Sources · Posts (см. схему)         │
  └──────────────────────────────────────────────────────┘
```

### Структура проекта

```
TgContentManager/
├── main.py                  # точка входа: связывает aiogram+Telethon+APScheduler
├── config.py                # PrimaryConfig (.env), пути, дефолты Settings
├── requirements.txt
├── .env.example
│
├── bot/                     # Telegram Admin Panel (aiogram 3.x)
│   ├── bot.py               # create_bot / create_dispatcher
│   ├── filters.py           # IsActiveUser, IsSuperAdmin, UserMiddleware
│   ├── keyboards.py         # Reply + Inline KB, CallbackData-фактори
│   ├── states.py            # FSM-группы
│   └── handlers/            # start · posts · sources · settings · ai_prompts · users
│
├── db/                      # слой персистентности
│   ├── database.py          # async engine, sessionmaker, init_db()
│   ├── models.py            # Settings · Sources · Posts
│   └── repositories.py      # CRUD + бизнес-выборки
│
├── services/                # бизнес-логика
│   ├── settings_service.py  # кэш настроек (TTL) + multi-user fallback
│   ├── llm_service.py       # обёртка над openai SDK (любой провайдер)
│   └── posts_service.py     # пайплайн: собери→скачай→LLM→сохрани→отправь
│
├── scraper/                 # коллекторы
│   ├── base.py              # CollectedItem + BaseCollector
│   ├── telegram_collector.py
│   ├── rss_collector.py
│   ├── github_collector.py
│   ├── newsdata_collector.py
│   └── manager.py           # фасад, per-source throttle
│
├── media/
│   └── downloader.py        # aiohttp + cleanup + classify_media
│
├── data/                    # content.db (gitignored)
├── tmp/                     # временные медиа (gitignored)
└── sessions/                # telethon.session (gitignored)
```

---

## ✅ Требования

- **ОС:** Ubuntu 20.04+ (тестировалось также на Windows 10/11 для разработки).
- **Python:** 3.10+ (рекомендуется 3.11 или 3.12).
- **RAM:** от 256 МБ (без медиа) до 1 ГБ (при активной обработке картинок).
- **Диск:** ~50 МБ под код и БД; `tmp/` очищается автоматически после каждой отправки.
- **Интернет:** исходящий HTTPS + доступ к Telegram API (порт 443).

---

## 🌱 Быстрый старт (локально)

```bash
# 1. Клонировать проект и войти в директорию
git clone <repo_url> TgContentManager
cd TgContentManager

# 2. Виртуальное окружение
python3 -m venv venv
source venv/bin/activate          # Linux/macOS
# venv\Scripts\activate           # Windows PowerShell

# 3. Зависимости
pip install -r requirements.txt

# 4. Скопировать шаблон окружения и заполнить первичные ключи
cp .env.example .env
nano .env                         # вписать BOT_TOKEN, ADMIN_IDS, TELETHON_API_ID/HASH

# 5. Первый запуск (с TTY — для интерактивной авторизации Telethon)
python main.py
```

После появления строки `Бот @... готов к работе` — бот работает, но **БД пустая**.
Дальнейшая настройка (AI-ключ, источники, ID канала) делается **через чат бота**.

---

## 🔑 Получение всех ключей и токенов

### 1. `BOT_TOKEN` — токен aiogram-бота
1. Открой `@BotFather` в Telegram.
2. `/newbot` → имя → username (должен заканчиваться на `bot`).
3. BotFather вернёт токен вида `123456:ABC-DEF...`.
4. Вставь в `.env` → `BOT_TOKEN=...`.

### 2. `ADMIN_IDS` — ID администраторов
1. Открой `@userinfobot` в Telegram.
2. Он пришлёт твой `Id: 123456789`.
3. Для нескольких админов — через запятую: `ADMIN_IDS=111,222,333`.

### 3. `TELETHON_API_ID` и `TELETHON_API_HASH` — для userbot'а
1. Зайди на <https://my.telegram.org> (с телефона).
2. `API development tools` → `App title` (любое) → `Short name` (любое) → `Platform: Desktop`.
3. Получишь `App api_id` (число) и `App api_hash` (строка).
4. Вставь в `.env` → `TELETHON_API_ID=12345`, `TELETHON_API_HASH=abc123...`.

> ⚠️ **ВАЖНО:** Telethon работает от имени **твоего личного аккаунта**. Не используй
> основной аккаунт — создай отдельный (finsta) для бота. Любой фрод-флаг на
> аккаунте userbot'а может привести к бану.

### 4. `ai_api_key` — API-ключ LLM-провайдера (получается ПОСЛЕ запуска, через чат бота)

Система поддерживает **любой OpenAI-совместимый API**:
- **Zhipu AI** (GLM-4-Flash, GLM-4): <https://open.bigmodel.cn/> — бесплатный тариф.
- **OpenAI**: <https://platform.openai.com/api-keys> — GPT-4, GPT-3.5.
- **Ollama** (локально): `http://localhost:11434/v1` — бесплатно, своё железо.
- **vLLM / Together AI / Anthropic через proxy** и др.

**По умолчанию** стоит Zhipu (base_url=`https://open.bigmodel.cn/api/paas/v4/`),
но можно переключить на любого провайдера через настройки бота.

Вставь ключ через бота: `⚙️ Настройки API → 🧪 API-ключ AI`.

> Бесплатный тариф GLM-4-Flash на момент написания — достаточно для MVP.

**Расширенное переопределение через `.env`** (опционально, deployment-level):

Если нужно закрепить провайдера или модель на уровне окружения (а не через чат),
`.env` поддерживает три переменные-оверрайда:

| Переменная | Что делает | По умолчанию |
|------------|-----------|--------------|
| `AI_BASE_URL` | Базовый URL API. Меняй при смене провайдера/использовании прокси. | Из БД (`https://open.bigmodel.cn/api/paas/v4/`) |
| `AI_MODEL_COLLECTOR` | Модель «Сборщика». Если задано — игнорирует значение из чата. | Из БД (`glm-4-flash`) |
| `AI_MODEL_WRITER` | Модель «Писателя». Если задано — игнорирует значение из чата. | Из БД (`glm-4`) |

**Приоритет разрешения модели:** `.env` → БД → hardcoded fallback.
Если переменная в `.env` пустая — работает значение из БД (меняется через чат).

### 5. `github_token` — Personal Access Token (опционально, через чат бота)
1. <https://github.com/settings/tokens> → `Generate new token (classic)`.
2. Достаточно **no scopes** (только публичные репо) — нужен только для повышения
   rate-limit с 60 до 5000 запросов/час.
3. Вставь через бота: `⚙️ Настройки API → 🐙 GitHub Token`.

### 6. `newsdata_api_key` — NewsData.io (опционально, через чат бота)
1. <https://newsdata.io/register> → бесплатный тариф = 200 запросов/день.
2. Скопируй API key из дашборда.
3. Вставь через бота: `⚙️ Настройки API → 📰 NewsData.io API Key`.

### 7. `target_channel_id` — куда публиковать одобренные посты
1. **Создай канал**, добавь в него бота **как администратора** с правом постинга.
2. Узнай ID канала: переслай любое сообщение канала в `@userinfobot`.
   Для каналов ID отрицательный и заканчивается на `-100...`: `-1001234567890`.
3. Вставь через бота: `⚙️ Настройки API → 📍 ID целевого канала`.

---

## 🤖 Первичная авторизация Telethon

Telethon требует **одноразовой** интерактивной авторизации (логин по номеру + код).
После неё создаётся файл `sessions/telethon.session`, и при последующих запусках
авторизация не требуется.

**Делается один раз на машине с TTY** (твой ноутбук или SSH-сессия):

```bash
source venv/bin/activate
python main.py
```

В консоли появится:

```
Please enter your phone (or bot token): +79991234567
Please enter the code you received: 12345
Please enter your password: (если включена 2FA)
```

После успешной авторизации:

```
Telethon запущен от имени @your_finsta_username (id=...).
Бот @your_bot готов к работе.
```

Можно остановить (`Ctrl+C`) и **забрать `sessions/telethon.session` на сервер** —
там авторизация больше не потребуется.

> ⚠️ **НЕ коммить** `sessions/telethon.session` в git — он уже в `.gitignore`.

---

## ⚙️ Настройка через бот (после запуска)

После `/start` в чате появится главное меню. Порядок первичной настройки:

| Шаг | Меню бота | Что вписать |
|----|-----------|-------------|
| 1 | `⚙️ Настройки API` → `🧪 API-ключ AI` | токен от LLM-провайдера (Zhipu/OpenAI/...) |
| 2 | `⚙️ Настройки API` → `📍 ID целевого канала` | `-1001234567890` |
| 3 | `⚙️ Настройки API` → `🎯 Мин. порог рейтинга (1-10)` | `6` (по умолчанию) |
| 4 | `⚙️ Настройки API` → `⏱ Интервал парсинга (мин)` | `60` (по умолчанию) |
| 5 | `📡 Источники` → `➕ RSS` | URL, напр. `https://www.reddit.com/r/Python.rss` |
| 6 | `📡 Источники` → `➕ TG-канал` | username без `@`, напр. `durov` |
| 7 | `🧠 Настройки ИИ` → `🔍 Промпт «Сборщик»` | свой системный промпт (опционально) |

Через минуту (один тик шедулера) бот начнёт присылать карточки-черновики.

---

## 🚢 Деплой на Ubuntu (systemd)

### 1. Установить Python 3.11+ и git
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### 2. Залить проект на сервер
```bash
git clone <repo_url> /opt/tgcm
cd /opt/tgcm
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Перенести `.env` и `sessions/telethon.session` с локальной машины
```bash
# С твоего ноутбука (замените user@server):
scp .env user@server:/opt/tgcm/.env
scp sessions/telethon.session user@server:/opt/tgcm/sessions/telethon.session
```

Без `telethon.session` на сервере бот стартует без TG-коллектора (RSS/GitHub/NewsData работают).

### 4. Создать systemd-юнит
```bash
sudo nano /etc/systemd/system/tgcm.service
```

Содержимое:
```ini
[Unit]
Description=TgContentManager bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/tgcm
EnvironmentFile=/opt/tgcm/.env
ExecStart=/opt/tgcm/venv/bin/python /opt/tgcm/main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 5. Запуск и автозапуск
```bash
sudo systemctl daemon-reload
sudo systemctl enable tgcm
sudo systemctl start tgcm
sudo systemctl status tgcm          # проверка состояния
sudo journalctl -u tgcm -f          # живые логи (Ctrl+C — выход)
```

### 6. Обновление кода
```bash
cd /opt/tgcm
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart tgcm
```

---

## 📱 Использование: разделы меню

После `/start` внизу чата — главное меню из 5 кнопок (4 для обычного пользователя,
5-я `👥 Пользователи` — только для супер-админа).

### 👥 Пользователи (только супер-админ)
Управление пользователями SaaS-системы:
- **📋 Список** — все зарегистрированные пользователи, их статус и дата регистрации.
- При нажатии на пользователя — inline-кнопки: ✅ Активировать / 🚫 Заблокировать / 🗑 Удалить.
- **Поток регистрации**: новый пользователь пишет `/start` → `status=pending` →
  супер-админ видит в списке → активирует → пользователь получает уведомление.

### 📊 База контента
- **🆕 Черновики** — листание свежих карточек (по одной, с пагинацией ◀️/▶️).
- **⭐ Топ контента** — топ-10 постов по рейтингу LLM.
- **📦 Архив** — отложенные посты, можно вернуть в черновики кнопкой ♻️.

**Карточка поста** содержит: рейтинг, источник, дату сбора, перевод/выжимку,
исходный текст (в `<blockquote>`), ссылку и inline-кнопки:

| Кнопка | Действие |
|--------|----------|
| ✅ Опубликовать | Пересылает в боевой канал, статус → `approved`. |
| 📦 В архив | Откладывает, статус → `archived`. |
| 🗑 Удалить | Помечает как мусор, статус → `rejected` (не вытирается из БД — для аудита). |
| ♻️ В черновики | Только для архивных: возвращает в очередь модерации. |
| ◀️ Пред. / След. ▶️ | Листание в пределах списка. |

### 📡 Источники
- **📋 Список источников** — все источники с кнопками выкл/вкл и удалить.
- **➕ TG-канал / ➕ RSS / ➕ GitHub / ➕ NewsData** — пошаговое добавление через FSM.

При добавлении:
- **Telegram:** username канала без `@` (напр. `durov`).
- **RSS:** полный URL (напр. `https://www.reddit.com/r/Python.rss`).
- **GitHub:** `owner/repo` (напр. `tiangolo/fastapi`).
- **NewsData:** поисковый запрос (напр. `AI OR "machine learning"`).

### ⚙️ Настройки API
Редактирование ключей и параметров. **Секреты маскируются** в превью:
`ai_api_key = sk-ab****xyz`. Полное значение видно только при вводе.

Каждый пользователь видит **только свои настройки**. Если пользователь не
переопределил настройку — работает системное значение по умолчанию.

### 🧠 Настройки ИИ
- Редактирование **системных промптов** «Сборщика» и «Писателя».
- Смена **моделей** (по умолчанию `glm-4-flash` и `glm-4`).
- Смена **провайдера**: измени `ai_base_url` на любой OpenAI-совместимый
  эндпоинт (OpenAI, Ollama `http://localhost:11434/v1`, vLLM, Together и др.).
- **Multi-user**: каждый пользователь имеет собственные промпты, модели и base_url.

> 💡 После любого изменения — кэш настроек сбрасывается мгновенно. Новый
> промпт/ключ подхватится уже на следующем тике шедулера (до 1 минуты).

---

## 🗄 Схема базы данных

Файл: `data/content.db` (SQLite). Четыре таблицы:

### `users`
| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int PK | — |
| `telegram_id` | int UNIQUE | Telegram ID пользователя |
| `username` | str | Username из Telegram |
| `full_name` | str | Отображаемое имя |
| `status` | enum | `pending` / `active` / `blocked` |
| `is_super_admin` | bool | true для админов из `ADMIN_IDS` |
| `created_at` | datetime | Дата регистрации |

Новый пользователь пишет `/start` → создаётся с `status=pending`.
Супер-админ активирует через `👥 Пользователи`.

### `settings` (KV-хранилище, multi-user)
| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int PK | — |
| `owner_id` | int FK | `users.id` (NULL = системный дефолт) |
| `key` | str UNIQUE | Напр. `ai_api_key` |
| `value` | text | Значение (всегда строка) |
| `description` | str | Человекочитаемое имя |
| `updated_at` | datetime | Авто-обновление |

**Приоритет**: пользовательская запись (`owner_id=user.id`) → системный дефолт
(`owner_id=NULL`). Если пользователь не задал своё значение — работает системное.

**Дефолтные ключи** (сидируются при первом запуске): `ai_api_key`,
`ai_base_url`, `ai_model_collector` (`glm-4-flash`), `ai_model_writer` (`glm-4`),
`github_token`, `newsdata_api_key`, `target_channel_id`,
`collector_interval_minutes` (60), `min_rating_threshold` (6),
`system_prompt_collector`, `system_prompt_writer`, `reddit_user_agent`.

### `sources` (multi-user)
| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int PK | — |
| `owner_id` | int FK | `users.id` (изоляция данных) |
| `type` | enum | `tg` / `rss` / `github` / `newsdata` |
| `identifier` | str | URL / username / `owner/repo` / query |
| `title` | str | Человекочитаемое имя |
| `enabled` | bool | Вкл/выкл |
| `extra` | text | JSON для доп. параметров |
| `created_at` / `last_fetched_at` | datetime | — |

### `posts` (multi-user)
| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int PK | — |
| `owner_id` | int FK | `users.id` (изоляция данных) |
| `source_id` | int FK | `sources.id` (SET NULL при удалении источника) |
| `source_url` | str | Ссылка на оригинал |
| `dedup_hash` | str UNIQUE per owner | SHA-256 от `url + raw_text` |
| `raw_text` | text | Оригинальный текст |
| `translated_text` | text | Перевод/выжимка от LLM |
| `media_file_id` | str? | Telegram file_id |
| `media_type` | str | `text` / `photo` / `video` / `animation` |
| `rating` | int | 1-10 (0 = не оценено) |
| `status` | enum | `draft` / `approved` / `rejected` / `archived` |
| `created_at` / `published_at` | datetime | — |

> Уникальность `dedup_hash` — в паре с `owner_id`. У разных пользователей
> могут быть одинаковые хэши (разные контексты).

---

## 🛠 Устранение неисправностей

### Бот не отвечает на `/start`
- Для **обычных пользователей**: бот отвечает всем — `/start` создаёт заявку.
- Если заявка не одобрена супер-админом — остальные команды недоступны.
- Если ты **супер-админ**, убедись, что твой ID есть в `ADMIN_IDS` в `.env`.
- В логах ищи `Бот @... готов к работе` — если её нет, упало на старте.

### `FATAL: BOT_TOKEN не задан`
Заполни `BOT_TOKEN` в `.env` и **перезапусти юнит** (`systemctl restart tgcm`).

### `Telethon не сконфигурирован`
`TELETHON_API_ID` или `TELETHON_API_HASH` пусты в `.env`. Без них TG-коллектор
не работает — только RSS/GitHub/NewsData.

### `Не удалось запустить Telethon: ...`
- Чаще всего: на сервере нет `sessions/telethon.session`.
- Решение: сгенерируй его локально (см. [Первичная авторизация Telethon](#-первичная-авторизация-telethon))
  и скопируй на сервер в `sessions/telethon.session`.
- **Никогда не запускай авторизацию в неинтерактивной среде systemd** — она зависнет.

### Бот шлёт карточки, но в канале не появляются одобренные
- Проверь `target_channel_id` в `⚙️ Настройки API`.
- Бот должен быть **администратором канала** с правом постинга.
- ID канала должен быть вида `-100...` (с префиксом `-100`).

### LLM-ответы пустые или `rating=0`
- Проверь `ai_api_key` в `⚙️ Настройки API` — маска должна быть не пустой.
- Убедись, что `ai_base_url` указывает на работающий эндпоинт `/v1/chat/completions`.
- Если ключ правильный, но рейтинги всё равно 0 — снизь `min_rating_threshold`
  в `⚙️ Настройки API` или подкорректируй `🔍 Промпт «Сборщик»`.

### Дубликаты постов
Не должны появляться — дедупликация по `dedup_hash`. Если всё же появились,
значит изменился `source_url` или `raw_text` (Telegram-пост отредактирован).

### БД разрослась
Удаляй старые `rejected` посты напрямую (в сервисе нет UI для этого):
```bash
sqlite3 /opt/tgcm/data/content.db "DELETE FROM posts WHERE status='rejected' AND created_at < datetime('now', '-30 days');"
```

### Хочу сбросить кэш настроек вручную
Перезапуск приложения: `sudo systemctl restart tgcm`. При старте `SettingsService`
загрузит свежие данные из БД.

---

## 🔧 Расширение проекта

### Добавить новый тип источника (например, Twitter/X)
1. В `db/models.py` — добавить значение в `SourceType` enum.
2. Создать `scraper/twitter_collector.py` — наследник `BaseCollector`.
3. В `scraper/manager.py` — зарегистрировать коллектор в `__init__`.
4. В `bot/keyboards.py` — добавить кнопку в `sources_menu_kb()`.
5. В `bot/handlers/sources.py` — обновить `SOURCE_TYPE_HINTS`.

Без миграции БД (enum хранится как строка, добавленные значения подхватятся).

### Подключить Redis для FSM (масштабирование)
В `bot/bot.py`:
```python
from aiogram.fsm.storage.redis import RedisStorage
storage = RedisStorage.from_url("redis://localhost:6379/0")
dp = Dispatcher(storage=storage)
```

### Подключить «Писателя» к UI
Метод `LLMService.write_post()` уже реализован. Добавь inline-кнопку в карточку
поста, в handler'е дёргай `write_post(post.translated_text)` и обновляй `translated_text`.

### Миграции Alembic (если меняешь схему)
В MVP используется `create_all()` — достаточно для старта. Для прод-эволюции:
```bash
pip install alembic
alembic init alembic
# в alembic/env.py настроить target_metadata = Base.metadata
alembic revision --autogenerate -m "describe change"
alembic upgrade head
```

---

## 📜 Лицензия

Проект распространяется как есть. Используй на свой страх и риск.
**Особое внимание** к ToS Telegram при работе через Telethon (userbot)
— избегай агрессивного парсинга и фрода.

## 🙋 Авторские права и контакты

При проблемах — смотри логи: `sudo journalctl -u tgcm -f --since "10 min ago"`.
