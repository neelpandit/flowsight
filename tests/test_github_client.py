from collections.abc import Callable

import httpx
import pytest
from pydantic import BaseModel

from flowsight.github.client import GitHubClient
from flowsight.github.errors import (
    GitHubAuthError,
    GitHubRateLimitError,
    GitHubResponseError,
    GitHubUnavailableError,
)

NOW = 1_000.0


class Viewer(BaseModel):
    login: str


class ViewerQuery(BaseModel):
    viewer: Viewer


OK = {"data": {"viewer": {"login": "octocat"}}}

Reply = httpx.Response | Exception


class Scripted:
    """Plays back one reply per request and records what was sent and how long we slept."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def client(self) -> GitHubClient:
        return GitHubClient(
            "test-token",
            transport=httpx.MockTransport(self.handler),
            sleep=self.sleep,
            now=lambda: NOW,
        )


def json(body: object, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


async def test_sends_the_token_and_returns_validated_data() -> None:
    scripted = Scripted(json(OK))
    result = await scripted.client().query("query { viewer { login } }", {}, ViewerQuery)
    assert result.viewer.login == "octocat"
    request = scripted.requests[0]
    assert str(request.url) == "https://api.github.com/graphql"
    assert request.headers["authorization"] == "Bearer test-token"


async def test_retries_5xx_and_html_error_pages() -> None:
    scripted = Scripted(httpx.Response(502, text="<html>Bad gateway</html>"), json(OK))
    await scripted.client().query("q", {}, ViewerQuery)
    assert len(scripted.sleeps) == 1


async def test_gives_up_after_repeated_timeouts_and_network_errors() -> None:
    scripted = Scripted(
        httpx.ReadTimeout("timed out"), httpx.ConnectError("reset"), httpx.ConnectError("reset")
    )
    with pytest.raises(GitHubUnavailableError):
        await scripted.client().query("q", {}, ViewerQuery)
    assert len(scripted.requests) == 3


async def test_waits_as_long_as_a_secondary_rate_limit_asks() -> None:
    scripted = Scripted(
        httpx.Response(
            403, text="You have exceeded a secondary rate limit", headers={"retry-after": "7"}
        ),
        json(OK),
    )
    await scripted.client().query("q", {}, ViewerQuery)
    assert scripted.sleeps == [7.0]


async def test_surfaces_a_rate_limit_that_resets_too_far_away() -> None:
    reset_in_an_hour = str(int(NOW) + 3_600)
    scripted = Scripted(
        json(
            {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset_in_an_hour},
        )
    )
    with pytest.raises(GitHubRateLimitError) as caught:
        await scripted.client().query("q", {}, ViewerQuery)
    assert caught.value.retry_after_seconds == 3_600
    assert scripted.sleeps == []


async def test_does_not_retry_a_rejected_token() -> None:
    scripted = Scripted(httpx.Response(401, text="Bad credentials"))
    with pytest.raises(GitHubAuthError):
        await scripted.client().query("q", {}, ViewerQuery)
    assert len(scripted.requests) == 1


class RepositoryQuery(BaseModel):
    class Repository(BaseModel):
        name: str

    repository: Repository | None


async def test_not_found_becomes_a_null_field_for_the_caller_to_handle() -> None:
    scripted = Scripted(
        json(
            {
                "data": {"repository": None},
                "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}],
            }
        )
    )
    result = await scripted.client().query("q", {}, RepositoryQuery)
    assert result.repository is None


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (lambda: json({"errors": [{"message": "Field 'foo' doesn't exist"}]}), "Field 'foo'"),
        (lambda: json({"data": {"viewer": {"id": 1}}}), "unexpected shape"),
    ],
)
async def test_rejects_other_graphql_errors_and_unexpected_shapes(
    reply: Callable[[], httpx.Response], message: str
) -> None:
    with pytest.raises(GitHubResponseError, match=message):
        await Scripted(reply()).client().query("q", {}, ViewerQuery)
