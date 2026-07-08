"""
bot/states.py
=============
FSM-состояния aiogram 3.x.

Каждая группа (StatesGroup) соответствует отдельному пошаговому сценарию:
    * AddSourceSG   — добавление нового источника (тип -> идентификатор -> заголовок)
    * EditSettingSG — ввод нового значения для настройки из EDITABLE_SETTINGS
    * EditPromptSG  — ввод нового системного промпта/модели

Состояния живут в MemoryStorage (см. bot/bot.py). На прод-нагрузке с
множеством админов разумно перейти на RedisStorage — это one-line-изменение.
"""
from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddSourceSG(StatesGroup):
    """
    Сценарий «📡 Источники -> ➕ Добавить».

    Шаг 1: choosing_type     — пользователь нажал кнопку типа (TG/RSS/...).
                               Тип уже зафиксирован в callback_data, поэтому
                               фактически этот стейт короткий — мы сразу просим
                               идентификатор.
    Шаг 2: waiting_identifier — пользователь вводит URL/@username/тег.
    Шаг 3: waiting_topics     — (только GitHub) пользователь вводит теги поиска
                               (topic:xxx), по одному на строку.
    Шаг 4: waiting_title       — (опц.) человекочитаемое имя источника.
    """

    choosing_type = State()
    waiting_identifier = State()
    waiting_topics = State()
    waiting_title = State()


class EditSettingSG(StatesGroup):
    """
    Сценарий «⚙️ Настройки API -> выбрать ключ -> ввести новое значение».

    Сам ключ сохраняем в FSM data, чтобы handler waiting_value знал что писать.
    """

    waiting_value = State()


class EditPromptSG(StatesGroup):
    """
    Сценарий «🧠 Настройки ИИ -> выбрать промпт -> ввести новый текст».

    Аналогично EditSettingSG: ключ промпта лежит в FSM data.
    """

    waiting_value = State()
