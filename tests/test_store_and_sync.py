from datetime import datetime

import pytest

from flowsight.domain import RepoRef
from flowsight.github.errors import GitHubUnavailableError, RepositoryNotFoundError
from flowsight.store import PullRequestStore, SyncState, open_database
from flowsight.sync import SyncResult, sync_repository
from tests.fakes import FakeGitHub, pull_request, remote, review, utc

REPO = RepoRef("acme", "widgets")
LONG_AGO = utc("2000-01-01T00:00")
OCTOBER = utc("2026-10-01T00:00")


@pytest.fixture
def store() -> PullRequestStore:
    store = PullRequestStore(open_database(":memory:"))
    store.ensure_repository(REPO.key, "acme/widgets")
    return store


# ---- store ---------------------------------------------------------------------------------


def test_round_trips_pull_requests_with_reviews(store: PullRequestStore) -> None:
    pr = pull_request(1, reviews=(review("bob", "APPROVED", "2026-10-02T09:00"),))
    store.save(REPO.key, [pr])
    assert store.pull_requests_since(REPO.key, LONG_AGO) == [pr]


def test_saving_again_replaces_reviews(store: PullRequestStore) -> None:
    store.save(
        REPO.key, [pull_request(1, reviews=(review("bob", "APPROVED", "2026-10-02T09:00"),))]
    )
    updated = pull_request(1, updated_at=utc("2026-10-03T10:00"))
    store.save(REPO.key, [updated])
    assert store.pull_requests_since(REPO.key, LONG_AGO) == [updated]


def test_returns_pull_requests_opened_or_closed_in_range(store: PullRequestStore) -> None:
    store.save(
        REPO.key,
        [
            pull_request(1, created_at=utc("2026-08-01T00:00"), merged_at=None, closed_at=utc("2026-08-02T00:00")),
            pull_request(2, created_at=utc("2026-08-01T00:00")),
            pull_request(3, created_at=utc("2026-10-04T00:00"), merged_at=None, closed_at=None),
        ],
    )  # fmt: skip
    assert [pr.number for pr in store.pull_requests_since(REPO.key, OCTOBER)] == [2, 3]


def test_has_no_sync_state_until_a_sync_completes(store: PullRequestStore) -> None:
    assert store.sync_state(REPO.key) is None
    state = SyncState(utc("2026-09-01T00:00"), utc("2026-11-08T12:00"))
    store.save_sync_state(REPO.key, state)
    assert store.sync_state(REPO.key) == state


# ---- sync ----------------------------------------------------------------------------------


async def sync(
    github: FakeGitHub, store: PullRequestStore, at: str, cover_from: datetime = OCTOBER
) -> SyncResult:
    return await sync_repository(github, store, REPO, cover_from, now=utc(at))


def stored_numbers(store: PullRequestStore) -> list[int]:
    return [pr.number for pr in store.pull_requests_since(REPO.key, LONG_AGO)]


async def test_first_sync_stores_pull_requests_opened_or_closed_in_range(
    store: PullRequestStore,
) -> None:
    github = FakeGitHub(
        [
            remote(1, "2026-10-05T00:00:00Z", "2026-10-07T00:00:00Z", "2026-10-07T00:00:00Z"),
            remote(2, "2026-09-20T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z"),
            remote(3, "2026-08-01T00:00:00Z", "2026-10-20T00:00:00Z", "2026-08-05T00:00:00Z"),
            remote(4, "2026-09-01T00:00:00Z", "2026-09-15T00:00:00Z"),
            remote(5, "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"),
        ]
    )
    result = await sync(github, store, "2026-11-08T12:00")
    # #3 was only commented on (it finished in August); #4 hasn't changed since September.
    assert result == SyncResult(4, 3, OCTOBER, utc("2026-11-08T12:00"))
    assert stored_numbers(store) == [1, 2, 5]


async def test_fetches_only_what_changed_since_the_previous_sync(store: PullRequestStore) -> None:
    github = FakeGitHub(
        [
            remote(1, "2026-10-05T00:00:00Z", "2026-10-07T00:00:00Z", "2026-10-07T00:00:00Z"),
            remote(2, "2026-11-08T09:00:00Z", "2026-11-08T11:58:00Z"),
        ]
    )
    await sync(github, store, "2026-11-08T12:00")
    github.pull_requests[0]["updatedAt"] = "2026-11-08T15:00:00Z"
    github.pull_requests.append(remote(3, "2026-11-08T14:00:00Z", "2026-11-08T14:00:00Z"))

    result = await sync(github, store, "2026-11-08T16:00")

    # #2 is re-listed because of the overlap window, but it is unchanged so it is not fetched.
    assert (result.listed, result.fetched, result.covered_from) == (3, 2, OCTOBER)
    assert github.detail_batches[-1] == [1, 3]


async def test_relists_further_back_when_an_earlier_start_is_needed(
    store: PullRequestStore,
) -> None:
    github = FakeGitHub(
        [
            remote(1, "2026-10-05T00:00:00Z", "2026-10-07T00:00:00Z", "2026-10-07T00:00:00Z"),
            remote(2, "2026-09-01T00:00:00Z", "2026-09-16T00:00:00Z", "2026-09-16T00:00:00Z"),
        ]
    )
    await sync(github, store, "2026-11-08T12:00")
    result = await sync(github, store, "2026-11-08T13:00", cover_from=utc("2026-09-15T00:00"))
    assert (result.listed, result.fetched, result.covered_from) == (2, 1, utc("2026-09-15T00:00"))
    assert stored_numbers(store) == [1, 2]


async def test_fetches_details_in_batches_of_25(store: PullRequestStore) -> None:
    github = FakeGitHub(
        [remote(n, "2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z") for n in range(1, 31)]
    )
    await sync(github, store, "2026-11-08T12:00")
    assert [len(batch) for batch in github.detail_batches] == [25, 5]


async def test_refuses_repositories_that_are_not_public(store: PullRequestStore) -> None:
    github = FakeGitHub([])
    github.is_private = True
    with pytest.raises(RepositoryNotFoundError):
        await sync(github, store, "2026-11-08T12:00")


async def test_records_no_coverage_when_a_sync_fails_part_way(store: PullRequestStore) -> None:
    github = FakeGitHub([remote(1, "2026-10-05T00:00:00Z", "2026-10-07T00:00:00Z")])
    github.fail_details = True
    with pytest.raises(GitHubUnavailableError):
        await sync(github, store, "2026-11-08T12:00")
    assert store.sync_state(REPO.key) is None
