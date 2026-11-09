import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from flowsight.github.errors import (
    GitHubAuthError,
    GitHubRateLimitError,
    GitHubResponseError,
    GitHubUnavailableError,
)

GRAPHQL_URL = "https://api.github.com/graphql"
# GitHub asks clients to wait at least a minute after a secondary rate limit with no hint.
DEFAULT_RATE_LIMIT_WAIT_S = 60.0

logger = logging.getLogger(__name__)


class _Retry(Exception):
    """Control flow inside the client; never escapes this module."""

    def __init__(self, after_s: float, error: Exception) -> None:
        self.after_s = after_s
        self.error = error


class GitHubClient:
    """A small GitHub GraphQL transport.

    The endpoint is fixed and inputs travel only as GraphQL variables, so request data can never
    redirect a call or change a query. The failures seen against real repositories (5xx and HTML
    error pages, timeouts, secondary rate limits) are retried with backoff; anything else fails
    fast with a typed error.
    """

    def __init__(
        self,
        token: str,
        *,
        timeout_s: float = 20.0,
        max_attempts: int = 3,
        max_rate_limit_wait_s: float = DEFAULT_RATE_LIMIT_WAIT_S,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._http = httpx.AsyncClient(
            headers={"authorization": f"Bearer {token}", "user-agent": "flowsight"},
            timeout=timeout_s,
            transport=transport,
        )
        self._max_attempts = max_attempts
        self._max_rate_limit_wait_s = max_rate_limit_wait_s
        self._sleep = sleep
        self._now = now

    async def aclose(self) -> None:
        await self._http.aclose()

    async def query[T: BaseModel](
        self, document: str, variables: dict[str, Any], model: type[T]
    ) -> T:
        for attempt in range(1, self._max_attempts + 1):
            try:
                payload = await self._attempt(document, variables, attempt)
            except _Retry as retry:
                if attempt == self._max_attempts or retry.after_s > self._max_rate_limit_wait_s:
                    raise retry.error from None
                logger.warning(
                    "retrying GitHub request in %.1fs (attempt %d): %s",
                    retry.after_s,
                    attempt,
                    retry.error,
                )
                await self._sleep(retry.after_s)
            else:
                return _parse_data(payload, model)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _attempt(self, document: str, variables: dict[str, Any], attempt: int) -> Any:
        try:
            response = await self._http.post(
                GRAPHQL_URL, json={"query": document, "variables": variables}
            )
        except httpx.TransportError as error:  # timeouts, resets, DNS failures
            raise _Retry(
                _backoff_s(attempt), GitHubUnavailableError("GitHub did not respond")
            ) from error
        logger.debug(
            "GitHub responded %d (rate limit remaining %s)",
            response.status_code,
            response.headers.get("x-ratelimit-remaining"),
        )

        if response.status_code == 401:
            raise GitHubAuthError("GitHub rejected the token (401)")
        if response.status_code in (403, 429):
            wait_s = self._rate_limit_wait_s(response)
            if wait_s is not None:
                raise _Retry(wait_s, _rate_limited(wait_s))
        if response.status_code >= 500:
            failure = GitHubUnavailableError(f"GitHub returned {response.status_code}")
            raise _Retry(_backoff_s(attempt), failure)
        if not response.is_success:
            raise GitHubResponseError(f"GitHub returned {response.status_code}")

        try:
            payload = response.json()
        except json.JSONDecodeError:
            failure = GitHubUnavailableError("GitHub returned a non-JSON body")
            raise _Retry(_backoff_s(attempt), failure) from None
        # An exhausted primary limit arrives as HTTP 200 with a RATE_LIMITED GraphQL error.
        if any(e.get("type") == "RATE_LIMITED" for e in _errors(payload)):
            wait_s = self._until_reset_s(response.headers)
            raise _Retry(wait_s, _rate_limited(wait_s))
        return payload

    def _rate_limit_wait_s(self, response: httpx.Response) -> float | None:
        """Secondary limits send `retry-after`; an exhausted primary limit sends a reset time."""
        retry_after = response.headers.get("retry-after", "")
        if retry_after.isdigit() and int(retry_after) > 0:
            return float(retry_after)
        if response.headers.get("x-ratelimit-remaining") == "0":
            return self._until_reset_s(response.headers)
        if response.status_code == 429 or re.search("rate limit", response.text, re.IGNORECASE):
            return DEFAULT_RATE_LIMIT_WAIT_S
        return None

    def _until_reset_s(self, headers: httpx.Headers) -> float:
        reset = headers.get("x-ratelimit-reset", "")
        if not reset.isdigit():
            return DEFAULT_RATE_LIMIT_WAIT_S
        return max(1.0, int(reset) - self._now())


def _rate_limited(wait_s: float) -> GitHubRateLimitError:
    return GitHubRateLimitError(int(wait_s + 0.999))


def _backoff_s(attempt: int) -> float:
    """Exponential backoff with jitter: 0.5-1 s, then 1-2 s, then 2-4 s."""
    ceiling = 2.0 ** (attempt - 1)
    return ceiling / 2 + random.random() * ceiling / 2  # noqa: S311 - jitter, not security


def _errors(payload: Any) -> list[dict[str, Any]]:
    errors = payload.get("errors") if isinstance(payload, dict) else None
    return [e for e in errors if isinstance(e, dict)] if isinstance(errors, list) else []


def _parse_data[T: BaseModel](payload: Any, model: type[T]) -> T:
    """NOT_FOUND surfaces as a null field (such as `repository: null`) for callers to handle."""
    if not isinstance(payload, dict):
        raise GitHubResponseError("GitHub returned an unexpected envelope")
    unexpected = [e for e in _errors(payload) if e.get("type") != "NOT_FOUND"]
    if unexpected:
        messages = "; ".join(str(e.get("message", "unknown error")) for e in unexpected)
        raise GitHubResponseError(f"GitHub returned errors: {messages}")
    try:
        return model.model_validate(payload.get("data"))
    except ValidationError as error:
        raise GitHubResponseError(f"GitHub returned an unexpected shape: {error}") from None
