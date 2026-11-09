import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from flowsight.domain import RepoRef
from flowsight.github.errors import RepositoryNotFoundError
from flowsight.github.pull_requests import GraphQLClient, lookup_repository
from flowsight.store import PullRequestStore
from flowsight.sync import sync_repository

# Stored data older than this is still served, but refreshed in the background.
STALE_AFTER = timedelta(minutes=10)
# After a failed sync, report the failure instead of hitting GitHub again until this passes.
RETRY_FAILED_AFTER = timedelta(seconds=30)

logger = logging.getLogger(__name__)


@dataclass
class SyncJob:
    repo: RepoRef
    started_at: datetime
    finished_at: datetime | None = None
    error: Exception | None = None

    @property
    def status(self) -> Literal["running", "succeeded", "failed"]:
        if self.finished_at is None:
            return "running"
        return "failed" if self.error else "succeeded"


@dataclass(frozen=True, slots=True)
class Ready:
    data_as_of: datetime
    refreshing: bool


@dataclass(frozen=True, slots=True)
class Pending:
    job: SyncJob


class SyncCoordinator:
    """Decides, per request, whether stored data can answer it, and runs syncs in the background.

    A first sync of a busy repository takes 40-145 s, far too long to hold an HTTP request, so
    callers get `Pending` and poll. At most one sync runs per repository; stale data keeps being
    served while it refreshes (stale-while-revalidate).
    """

    def __init__(
        self,
        github: GraphQLClient,
        store: PullRequestStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._github = github
        self._store = store
        self._clock = clock
        self._jobs: dict[str, SyncJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def job(self, repo: RepoRef) -> SyncJob | None:
        return self._jobs.get(repo.key)

    async def ensure(self, repo: RepoRef, need_from: datetime) -> Ready | Pending:
        now = self._clock()
        state = self._store.sync_state(repo.key)
        job = self._jobs.get(repo.key)
        running = job is not None and job.status == "running"
        recently_failed = (
            job is not None
            and job.finished_at is not None
            and job.error is not None
            and now - job.finished_at < RETRY_FAILED_AFTER
        )

        if state is not None and state.covered_from <= need_from:
            if not running and not recently_failed and now - state.synced_at > STALE_AFTER:
                self._start(repo, state.covered_from)
                running = True
            return Ready(state.synced_at, refreshing=running)
        if job is not None and running:
            return Pending(job)
        if job is not None and job.error is not None and recently_failed:
            raise job.error
        # Check the repository exists before accepting work, so typos fail fast with a 404.
        info = await lookup_repository(self._github, repo)
        if info is None or info.is_private:
            raise RepositoryNotFoundError(repo)
        return Pending(self._start(repo, need_from))

    def _start(self, repo: RepoRef, cover_from: datetime) -> SyncJob:
        job = SyncJob(repo, self._clock())
        self._jobs[repo.key] = job
        task = asyncio.create_task(self._run(job, cover_from))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run(self, job: SyncJob, cover_from: datetime) -> None:
        try:
            await sync_repository(
                self._github, self._store, job.repo, cover_from, now=job.started_at
            )
        except Exception as error:  # recorded and reported to the next caller
            job.error = error
            logger.warning("sync of %s failed: %s", job.repo.key, error)
        finally:
            job.finished_at = self._clock()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
