import asyncio
from datetime import datetime, timedelta

import pytest

from flowsight.api.sync_jobs import Pending, Ready, SyncCoordinator, SyncJob
from flowsight.domain import RepoRef
from flowsight.github.errors import GitHubUnavailableError, RepositoryNotFoundError
from flowsight.store import PullRequestStore, SyncState, open_database
from tests.fakes import FakeGitHub, remote, utc

REPO = RepoRef("acme", "widgets")
NEED_FROM = utc("2026-10-01T00:00")


class Clock:
    def __init__(self, now: str) -> None:
        self.now = utc(now)

    def __call__(self) -> datetime:
        return self.now


async def finished(job: SyncJob) -> SyncJob:
    for _ in range(100):
        if job.status != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("sync job did not finish")


@pytest.fixture
def store() -> PullRequestStore:
    return PullRequestStore(open_database(":memory:"))


def coordinator(github: FakeGitHub, store: PullRequestStore, clock: Clock) -> SyncCoordinator:
    return SyncCoordinator(github, store, clock=clock)


async def test_first_request_starts_a_background_sync_then_data_is_ready(
    store: PullRequestStore,
) -> None:
    clock = Clock("2026-11-08T12:00")
    sync = coordinator(
        FakeGitHub([remote(1, "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z")]), store, clock
    )

    pending = await sync.ensure(REPO, NEED_FROM)
    assert isinstance(pending, Pending)
    assert (await finished(pending.job)).status == "succeeded"

    assert await sync.ensure(REPO, NEED_FROM) == Ready(clock.now, refreshing=False)


async def test_concurrent_requests_share_one_sync(store: PullRequestStore) -> None:
    sync = coordinator(FakeGitHub([]), store, Clock("2026-11-08T12:00"))
    first, second = await asyncio.gather(sync.ensure(REPO, NEED_FROM), sync.ensure(REPO, NEED_FROM))
    assert isinstance(first, Pending)
    assert isinstance(second, Pending)
    assert first.job is second.job


async def test_unknown_or_private_repositories_fail_fast(store: PullRequestStore) -> None:
    github = FakeGitHub([])
    github.is_private = True
    with pytest.raises(RepositoryNotFoundError):
        await coordinator(github, store, Clock("2026-11-08T12:00")).ensure(REPO, NEED_FROM)


async def test_stale_data_is_served_while_it_refreshes(store: PullRequestStore) -> None:
    store.ensure_repository(REPO.key, "acme/widgets")
    synced_at = utc("2026-11-08T11:00")
    store.save_sync_state(REPO.key, SyncState(NEED_FROM, synced_at))
    clock = Clock("2026-11-08T11:05")
    sync = coordinator(FakeGitHub([]), store, clock)

    assert await sync.ensure(REPO, NEED_FROM) == Ready(synced_at, refreshing=False)
    clock.now += timedelta(hours=1)
    assert await sync.ensure(REPO, NEED_FROM) == Ready(synced_at, refreshing=True)


async def test_a_failed_sync_is_reported_then_retried_after_a_cooldown(
    store: PullRequestStore,
) -> None:
    github = FakeGitHub([remote(1, "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z")])
    github.fail_details = True
    clock = Clock("2026-11-08T12:00")
    sync = coordinator(github, store, clock)

    pending = await sync.ensure(REPO, NEED_FROM)
    assert isinstance(pending, Pending)
    await finished(pending.job)
    with pytest.raises(GitHubUnavailableError):
        await sync.ensure(REPO, NEED_FROM)

    clock.now += timedelta(minutes=1)
    retry = await sync.ensure(REPO, NEED_FROM)
    assert isinstance(retry, Pending)
    assert retry.job is not pending.job
