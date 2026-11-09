import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from flowsight.api.app import create_app
from flowsight.config import Settings
from flowsight.github.errors import GitHubRateLimitError
from flowsight.store import PullRequestStore, SyncState, open_database
from tests.fakes import FakeGitHub, pull_request, remote, review, utc

NOW = utc("2026-11-08T12:30")
FLOW = "/v1/repos/acme/widgets/flow"


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, github_token=SecretStr("test-token"), **overrides)


def seeded_store() -> PullRequestStore:
    """acme/widgets, synced and covering September onwards."""
    store = PullRequestStore(open_database(":memory:"))
    store.ensure_repository("acme/widgets", "Acme/Widgets")
    store.save(
        "acme/widgets",
        [
            pull_request(
                n, created_at=utc(f"2026-10-{n + 10}T09:00"), ready_at=utc(f"2026-10-{n + 10}T09:00"),
                merged_at=utc(f"2026-10-{n + 11}T09:00"), closed_at=utc(f"2026-10-{n + 11}T09:00"),
                updated_at=utc(f"2026-10-{n + 11}T09:00"),
                reviews=(review("bob", "APPROVED", f"2026-10-{n + 10}T{9 + n:02d}:00"),),
            )
            for n in range(1, 6)
        ],
    )  # fmt: skip
    store.save_sync_state(
        "acme/widgets", SyncState(utc("2026-09-01T00:00"), utc("2026-11-08T12:25"))
    )
    return store


@pytest.fixture
def client() -> Iterator[TestClient]:
    store = seeded_store()
    app = create_app(settings(), github=FakeGitHub([]), connection=store._db, clock=lambda: NOW)
    with TestClient(app) as test_client:
        yield test_client


def test_flow_reports_metrics_and_signals_for_the_default_period(client: TestClient) -> None:
    response = client.get(FLOW)
    assert response.status_code == 200
    body = response.json()
    assert body["repository"] == "Acme/Widgets"
    assert body["period"] == {"start": "2026-10-11T12:00:00Z", "end": "2026-11-08T12:00:00Z"}
    assert body["current"]["merged"] == 5
    assert body["current"]["stages"]["wait_for_first_review"]["p50"] == 3
    assert {s["metric"] for s in body["signals"]} >= {"merged_pull_requests", "top_reviewer_share"}
    assert response.headers["x-content-type-options"] == "nosniff"


def test_repeat_requests_can_be_answered_with_not_modified(client: TestClient) -> None:
    etag = client.get(FLOW).headers["etag"]
    repeat = client.get(FLOW, headers={"if-none-match": etag})
    assert repeat.status_code == 304
    assert repeat.content == b""


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        ("/v1/repos/-bad/widgets/flow", "owner and repository"),
        (f"{FLOW}?from=2026-11-01&to=2026-10-01", "earlier"),
        (f"{FLOW}?from=2026-11-01&to=2026-12-01", "future"),
        (f"{FLOW}?from=2026-06-01&to=2026-10-01", "at most 90 days"),
        (f"{FLOW}?from=yesterday", "from"),
    ],
)
def test_invalid_input_is_a_400_problem(client: TestClient, url: str, detail: str) -> None:
    response = client.get(url)
    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"
    assert detail in response.json()["detail"]
    assert "yesterday" not in response.text  # input values are never echoed


def test_repositories_outside_the_allowlist_are_refused() -> None:
    app = create_app(
        settings(allowed_repos="acme/other"), github=FakeGitHub([]),
        connection=seeded_store()._db, clock=lambda: NOW,
    )  # fmt: skip
    with TestClient(app) as client:
        assert client.get(FLOW).status_code == 403


def test_private_or_unknown_repositories_are_a_404() -> None:
    github = FakeGitHub([])
    github.is_private = True
    app = create_app(settings(), github=github, connection=seeded_store()._db, clock=lambda: NOW)
    with TestClient(app) as client:
        response = client.get("/v1/repos/acme/secret/flow")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_first_request_for_a_repository_is_accepted_and_synced_in_the_background() -> None:
    github = FakeGitHub(
        [remote(1, "2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z", "2026-10-21T00:00:00Z")]
    )
    app = create_app(
        settings(), github=github, connection=open_database(":memory:"), clock=lambda: NOW
    )
    with TestClient(app) as client:
        accepted = client.get(FLOW)
        assert accepted.status_code == 202
        assert accepted.headers["retry-after"] == "10"
        status_url = accepted.headers["location"]
        assert status_url == "/v1/repos/acme/widgets/sync"

        for _ in range(100):
            job = client.get(status_url).json()["job"]
            if job["status"] != "running":
                break
            time.sleep(0.01)
        assert job["status"] == "succeeded"
        assert client.get(FLOW).json()["current"]["merged"] == 1


def test_github_rate_limits_become_a_503_with_retry_after() -> None:
    class RateLimited(FakeGitHub):
        async def query[T: BaseModel](
            self, document: str, variables: dict[str, Any], model: type[T]
        ) -> T:
            raise GitHubRateLimitError(120)

    app = create_app(
        settings(), github=RateLimited([]), connection=open_database(":memory:"), clock=lambda: NOW
    )
    with TestClient(app) as client:
        response = client.get(FLOW)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "120"


def test_health_endpoint(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_the_ui_is_served_with_a_strict_content_security_policy(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert '<script type="module" src="/static/app.js">' in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
    assert "content-security-policy" not in client.get("/healthz").headers
