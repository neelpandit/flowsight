import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from flowsight.domain import RepoRef
from flowsight.github.errors import RepositoryNotFoundError
from flowsight.github.pull_requests import (
    GraphQLClient,
    PullRequestSummary,
    fetch_pull_requests,
    list_pull_requests_updated_since,
    lookup_repository,
)
from flowsight.store import PullRequestStore, SyncState

# Keeps each details query cheap and quick (about 1.6 s per batch against real repositories).
DETAIL_BATCH_SIZE = 25
# Re-list a few minutes before the previous sync to absorb clock skew and in-flight updates.
RESYNC_OVERLAP = timedelta(minutes=5)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncResult:
    listed: int  # pull requests seen in the updated-since listing
    fetched: int  # pull requests whose details were fetched because they were new or changed
    covered_from: datetime
    synced_at: datetime


async def sync_repository(
    github: GraphQLClient,
    store: PullRequestStore,
    repo: RepoRef,
    cover_from: datetime,
    *,
    now: datetime | None = None,
) -> SyncResult:
    """Brings stored data for `repo` up to date and guarantees coverage back to `cover_from`:
    every pull request opened or closed since then is stored with its reviews.

    Fetching pull requests together with their reviews was slow and tripped GitHub's secondary
    rate limits, so this runs in two phases: a cheap listing ordered by last update finds what
    changed, then details are fetched only for in-range pull requests whose stored copy is
    stale. Coverage is recorded last, so an interrupted sync never claims data it does not
    have, and a rerun skips whatever the interrupted one already stored.
    """
    synced_at = now or datetime.now(UTC)
    info = await lookup_repository(github, repo)
    if info is None or info.is_private:
        raise RepositoryNotFoundError(repo)
    store.ensure_repository(repo.key, info.name_with_owner)

    # Once the range is covered, only changes since the previous sync need listing.
    previous = store.sync_state(repo.key)
    incremental = previous is not None and previous.covered_from <= cover_from
    covered_from = previous.covered_from if previous and incremental else cover_from
    list_since = previous.synced_at - RESYNC_OVERLAP if previous and incremental else cover_from

    listed = 0
    in_range: list[PullRequestSummary] = []
    async for summary in list_pull_requests_updated_since(github, repo, list_since):
        listed += 1
        if _in_range(summary, covered_from):
            in_range.append(summary)
    stored = store.updated_at_by_number(repo.key, [summary.number for summary in in_range])
    changed = [s for s in in_range if stored.get(s.number) != s.updated_at]

    for start in range(0, len(changed), DETAIL_BATCH_SIZE):
        batch = changed[start : start + DETAIL_BATCH_SIZE]
        fetched = await fetch_pull_requests(github, [summary.id for summary in batch])
        if fetched.reviews_truncated:
            logger.warning(
                "%s: stored only the first 100 reviews of %s", repo.key, fetched.reviews_truncated
            )
        store.save(repo.key, fetched.pull_requests)
        logger.debug("%s: fetched %d of %d", repo.key, start + len(batch), len(changed))

    store.save_sync_state(repo.key, SyncState(covered_from, synced_at))
    result = SyncResult(listed, len(changed), covered_from, synced_at)
    logger.info("%s: sync complete, listed %d, fetched %d", repo.key, listed, len(changed))
    return result


def _in_range(summary: PullRequestSummary, covered_from: datetime) -> bool:
    """Only pull requests opened or closed inside the covered range matter for analysis."""
    closed_in_range = summary.closed_at is not None and summary.closed_at >= covered_from
    return summary.created_at >= covered_from or closed_in_range
