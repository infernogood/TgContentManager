"""
services/llm_service.py
=======================
Универсальный провайдер-агностичный клиент LLM.

ЗАМЕНА ZhipuService: теперь работает с ЛЮБЫМ OpenAI-совместимым API:
    * Zhipu AI (по умолчанию, base_url=https://open.bigmodel.cn/api/paas/v4/)
    * OpenAI (https://api.openai.com/v1)
    * Anthropic через OpenAI-прокси
    * Локальные LLM (Ollama, vLLM, LM Studio — base_url=http://localhost:11434/v1)

Multi-user: каждый юзер использует свои api_key/base_url/модель из Settings.
Клиенты кэшируются по (user_id, api_key, base_url) — рекреация при смене ключа.

Контракт методов изменился: все async-методы принимают user_id первым аргументом.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from services.settings_service import SettingsService

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Pydantic-схема ответа сборщика
# --------------------------------------------------------------------------- #
class AnalysisResult(BaseModel):
    """rating=0 — SENTINEL 'не оценено' (см. zhipu_service ранее)."""

    rating: int = Field(ge=0, le=10)
    summary: str = Field(default="")


FALLBACK_RESULT = AnalysisResult(rating=0, summary="")


class LLMService:
    """Async LLM-клиент над openai SDK с поддержкой любого провайдера."""

    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings
        # Кэш клиентов: user_id -> (AsyncOpenAI, fingerprint).
        # fingerprint = (api_key, base_url). Если юзер сменил ключ/URL —
        # клиент пересоздаётся автоматически на следующем вызове.
        self._clients: dict[int, tuple[AsyncOpenAI, tuple[str, str]]] = {}

    # ------------------------------------------------------------------ #
    #  Управление клиентом (per-user)
    # ------------------------------------------------------------------ #
    async def _get_client(self, user_id: int) -> AsyncOpenAI:
        """Создаёт или возвращает кэшированного клиента для юзера."""
        api_key = await self.settings.get_str(user_id, "ai_api_key")
        if not api_key:
            raise RuntimeError(
                "ai_api_key не задан. Настрой через ⚙️ Настройки API в боте."
            )
        base_url = await self.settings.get_str(
            user_id, "ai_base_url", "https://open.bigmodel.cn/api/paas/v4/"
        )

        fingerprint = (api_key, base_url)
        cached = self._clients.get(user_id)
        if cached is None or cached[1] != fingerprint:
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            self._clients[user_id] = (client, fingerprint)
            log.info(
                "LLM-клиент (пере)создан для user=%s, base_url=%s", user_id, base_url
            )
        return self._clients[user_id][0]

    def invalidate_client(self, user_id: int) -> None:
        """Сброс кэша клиента (после смены api_key/base_url)."""
        self._clients.pop(user_id, None)

    # ------------------------------------------------------------------ #
    #  Анализ (Сборщик)
    # ------------------------------------------------------------------ #
    async def analyze(self, user_id: int, raw_text: str) -> AnalysisResult:
        """Оценка релевантности (1-10) + перевод/выжимка."""
        if not raw_text or not raw_text.strip():
            return FALLBACK_RESULT

        prompt = await self.settings.get_str(user_id, "system_prompt_collector")
        model = await self.settings.get_str(user_id, "ai_model_collector", "glm-4-flash")

        try:
            client = await self._get_client(user_id)
            # Добавляем user-instruction в конец для усиления JSON-формата
            messages = [
                {"role": "system", "content": prompt},
                {"role": "user", "content": raw_text[:8000]},
                {"role": "user", "content": 'Ответи ТОЛЬКО JSON: {"rating": N, "summary": "текст"}'},
            ]
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                    response_format={"type": "json_object"},
                )
            except Exception:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                )
            content = response.choices[0].message.content or ""
            return self._parse_analysis(content)
        except RuntimeError:
            # Нет api_key — пробрасываем для логирования выше.
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("LLM analyze failed (user=%s): %s", user_id, exc)
            return FALLBACK_RESULT

    # ------------------------------------------------------------------ #
    #  Написание поста (Писатель)
    # ------------------------------------------------------------------ #
    async def write_post(self, user_id: int, material: str) -> str:
        """Готовый пост через writer-модель."""
        if not material.strip():
            return ""

        prompt = await self.settings.get_str(user_id, "system_prompt_writer")
        model = await self.settings.get_str(user_id, "ai_model_writer", "glm-4")

        try:
            client = await self._get_client(user_id)
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": material[:12000]},
                ],
                temperature=0.7,
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001
            log.exception("LLM write_post failed (user=%s): %s", user_id, exc)
            return ""

    # ------------------------------------------------------------------ #
    #  Парсинг ответа LLM (без изменений по сравнению с ZhipuService)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_analysis(content: str) -> AnalysisResult:
        if not content:
            return FALLBACK_RESULT

        # 1. Прямой JSON
        try:
            return AnalysisResult.model_validate_json(content)
        except (json.JSONDecodeError, ValidationError):
            pass

        # 2. JSON в code fence ```json ... ```
        fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if fence_match:
            try:
                return AnalysisResult.model_validate_json(fence_match.group(1))
            except (json.JSONDecodeError, ValidationError):
                pass

        # 3. JSON где-то в тексте (с "rating")
        brace_match = re.search(r"\{[^{}]*\"rating\"[^{}]*\}", content, re.DOTALL)
        if brace_match:
            try:
                return AnalysisResult.model_validate_json(brace_match.group(0))
            except (json.JSONDecodeError, ValidationError):
                pass

        # 4. Паттерны "rating: N" / "рейтинг: N" / "оценка: N"
        rating_kws = re.search(
            r'(?:rating|рейтинг|оценка|score)\s*[:=]\s*(\d{1,2})',
            content, re.IGNORECASE,
        )
        if rating_kws:
            rating = max(0, min(10, int(rating_kws.group(1))))
            return AnalysisResult(rating=rating, summary=content[:2000])

        # 5. Извлекаем число 1-10 из ПЕРВОЙ строки
        first_line = content.strip().splitlines()[0] if content.strip() else ""
        rating_match = re.search(r"\b([1-9]|10)\b", first_line)
        if rating_match:
            rating = int(rating_match.group(1))
            summary = content[len(first_line):].strip() or content.strip()
            return AnalysisResult(rating=rating, summary=summary[:2000])

        # 6. Fallback: текст есть, но рейтинга нет — ставим нейтральный 5
        #    (не 0, чтобы не отбрасывать каждый пост)
        if len(content.strip()) > 20:
            log.warning(
                "LLM-ответ без рейтинга, default rating=5: %r", content[:200]
            )
            return AnalysisResult(rating=5, summary=content[:2000])

        log.warning("LLM-ответ не распарсен: %r", content[:200])
        return FALLBACK_RESULT
