"""Thin OpenAI chat-completions client with retries."""
from __future__ import annotations

import logging
import time

from config.policy import settings

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


class OpenAIChat:
    def __init__(self, model: str | None = None, api_key: str | None = None, max_retries: int = 2):
        api_key = api_key or settings.openai_api_key
        if not api_key:
            raise RuntimeError("POLICY_OPENAI_API_KEY (or OPENAI_API_KEY) is not set")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, timeout=30)
        self.model = model or settings.openai_model
        self._max_retries = max_retries

    def complete(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=settings.llm_temperature if temperature is None else temperature,
                    max_tokens=max_tokens or settings.llm_max_tokens,
                )
                return (resp.choices[0].message.content or "").strip()
            except Exception as exc:  # network / rate-limit / 5xx
                last_exc = exc
                logger.warning("LLM call failed (attempt %d): %s", attempt + 1, exc)
                if attempt < self._max_retries:
                    time.sleep(1.5 * (attempt + 1))
        raise LLMError(str(last_exc)) from last_exc
