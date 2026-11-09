import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class LlmError(Exception):
    """The model could not produce a usable answer (unreachable, refused, or malformed)."""


class InsightModel(Protocol):
    """Anything that turns a prompt into an answer matching a pydantic schema."""

    name: str

    async def generate[T: BaseModel](self, system: str, user: str, schema: type[T]) -> T: ...


class AllModelsUnavailableError(LlmError):
    """Every configured model was unavailable; the message lists why."""


async def with_backups[T](
    models: Sequence[InsightModel], call: Callable[[InsightModel], Awaitable[T]]
) -> tuple[T, InsightModel]:
    """Runs `call` with the first model that is available. Quotas are per model, so when one is
    rate limited or down, the next often still answers. Only `LlmError` moves on: an answer that
    fails validation is the caller's business, not a reason to ask another model."""
    reasons = []
    for model in models:
        try:
            return await call(model), model
        except LlmError as error:
            logger.warning("%s unavailable: %s", model.name, error)
            reasons.append(f"{model.name} unavailable: {error}")
    raise AllModelsUnavailableError("; ".join(reasons) or "no model configured")


class OpenAICompatibleModel:
    """Talks to any OpenAI-compatible chat API: Google Gemini (the free default), Ollama for a
    fully local setup, Groq, OpenRouter or OpenAI itself. The response must match the requested
    schema (JSON-schema structured output); anything else is an `LlmError`."""

    def __init__(self, *, api_key: str, base_url: str, model: str, timeout_s: float = 90) -> None:
        self.name = model
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=2
        )

    async def generate[T: BaseModel](self, system: str, user: str, schema: type[T]) -> T:
        try:
            completion = await self._client.chat.completions.parse(
                model=self.name,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format=schema,
            )
        except (openai.APIError, openai.LengthFinishReasonError) as error:
            raise LlmError(f"{type(error).__name__}: {error}") from error
        message = completion.choices[0].message
        if message.parsed is None:
            raise LlmError(f"model returned no structured answer (refusal: {message.refusal!r})")
        return message.parsed

    async def aclose(self) -> None:
        await self._client.close()
