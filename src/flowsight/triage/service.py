import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from flowsight.analytics.metrics import Period
from flowsight.domain import PullRequest, RepoRef, to_iso
from flowsight.github.conversations import fetch_conversations
from flowsight.github.pull_requests import GraphQLClient
from flowsight.insights.llm import AllModelsUnavailableError, InsightModel, with_backups
from flowsight.store import PullRequestStore
from flowsight.triage.casefile import (
    CaseFile,
    build_case_file,
    pick_pull_requests,
    team_members,
)
from flowsight.triage.prompt import PROMPT_VERSION, SYSTEM_PROMPT, user_message
from flowsight.triage.schema import Diagnosis, QuoteView, TriageAnswer, TriagedPullRequest
from flowsight.triage.validation import validate_diagnosis

logger = logging.getLogger(__name__)

FETCH_BATCH = 7  # conversations per GraphQL query
READ_BATCH = 11  # conversations per model call: two calls cover a full reading
PARALLEL_CALLS = 2  # the free tier allows only a few calls a minute


@dataclass(frozen=True, slots=True)
class Triage:
    pull_requests: list[TriagedPullRequest]
    prompt_version: str
    models: list[str]  # the models that wrote these diagnoses
    cached: int  # diagnoses reused from the cache
    not_shown: list[str]  # pull requests left out, and why


class TriageService:
    """Reads the conversations of the pull requests that waited longest and asks the model why
    each one waited. Facts come from code; the model's reading is checked (quotes verbatim,
    owners from the thread or the team, numbers from the case file) before it is shown or
    cached. Backup models are tried only when a model is unavailable."""

    def __init__(
        self,
        models: Sequence[InsightModel],
        store: PullRequestStore,
        github: GraphQLClient,
    ) -> None:
        self._models = list(models)
        self._store = store
        self._github = github
        self._calls = asyncio.Semaphore(PARALLEL_CALLS)

    async def triage(
        self,
        repo: RepoRef,
        repository: str,
        pull_requests: Sequence[PullRequest],
        period: Period,
        now: datetime,
    ) -> Triage:
        picks = pick_pull_requests(pull_requests, period, now)
        team = team_members(pull_requests)
        found: dict[int, TriagedPullRequest] = {}
        for pr in picks:
            cached = self._cached(repo, pr, now)
            if cached is not None:
                found[pr.number] = cached
        reused = len(found)

        cases = await self._case_files(
            repo, [pr for pr in picks if pr.number not in found], team, now
        )
        batches = [cases[i : i + READ_BATCH] for i in range(0, len(cases), READ_BATCH)]
        results = await asyncio.gather(*(self._read(repository, batch) for batch in batches))
        not_shown: list[str] = []
        for diagnosed, problems in results:
            not_shown += problems
            for case, model, diagnosis in diagnosed:
                item = _triaged(case, diagnosis, model)
                found[case.number] = item
                self._store.save_insight(
                    _cache_key(repo, case.pull_request, now, model), model, PROMPT_VERSION,
                    item.model_dump_json(),
                )  # fmt: skip
        ordered = [found[pr.number] for pr in picks if pr.number in found]
        return Triage(
            ordered,
            PROMPT_VERSION,
            sorted({item.model for item in ordered}),
            reused,
            not_shown,
        )

    def _cached(self, repo: RepoRef, pr: PullRequest, now: datetime) -> TriagedPullRequest | None:
        for model in self._models:
            body = self._store.cached_insight(_cache_key(repo, pr, now, model.name))
            if body is not None:
                return TriagedPullRequest.model_validate_json(body)
        return None

    async def _case_files(
        self, repo: RepoRef, picks: Sequence[PullRequest], team: list[str], now: datetime
    ) -> list[CaseFile]:
        by_number = {pr.number: pr for pr in picks}
        numbers = list(by_number)
        chunks = [numbers[i : i + FETCH_BATCH] for i in range(0, len(numbers), FETCH_BATCH)]
        fetched = await asyncio.gather(
            *(fetch_conversations(self._github, repo, chunk) for chunk in chunks)
        )
        return [
            build_case_file(by_number[conversation.number], conversation, team, now)
            for conversations in fetched
            for conversation in conversations
            # GitHub is fresher than the last sync: skip what was since closed or made a draft.
            if conversation.state == "OPEN" and not conversation.is_draft
        ]

    async def _read(
        self, repository: str, cases: list[CaseFile]
    ) -> tuple[list[tuple[CaseFile, str, Diagnosis]], list[str]]:
        """Diagnoses that passed the checks, and the pull requests left out with the reason."""
        try:
            async with self._calls:
                result, _ = await with_backups(
                    self._models, lambda model: self._diagnose(model, repository, cases)
                )
        except AllModelsUnavailableError as error:
            return [], [f"#{case.number}: {error}" for case in cases]
        return result

    async def _diagnose(
        self, model: InsightModel, repository: str, cases: list[CaseFile]
    ) -> tuple[list[tuple[CaseFile, str, Diagnosis]], list[str]]:
        prompt = user_message(repository, cases)
        answer = await model.generate(SYSTEM_PROMPT, prompt, TriageAnswer)
        passed, failing = _check(answer, cases)
        if not failing:
            return [(c, model.name, d) for c, d in passed], []

        logger.info("triage failed validation, asking for a repair: %s", failing)
        retry = [case for case in cases if case.number in failing]
        problems = "\n".join(f"- {issue}" for issues in failing.values() for issue in issues)
        repair = (
            f"{user_message(repository, retry)}\n\nA previous answer failed these checks. "
            f"Diagnose these pull requests again and fix them:\n{problems}"
        )
        repaired, still_failing = _check(
            await model.generate(SYSTEM_PROMPT, repair, TriageAnswer), retry
        )
        return [(c, model.name, d) for c, d in [*passed, *repaired]], [
            f"#{number}: failed checks: {'; '.join(issues)}"
            for number, issues in still_failing.items()
        ]


def _check(
    answer: TriageAnswer, cases: Sequence[CaseFile]
) -> tuple[list[tuple[CaseFile, Diagnosis]], dict[int, list[str]]]:
    """Splits an answer into diagnoses that pass and, per pull request, the checks that fail."""
    by_number = {d.pull_request: d for d in answer.diagnoses}
    passed: list[tuple[CaseFile, Diagnosis]] = []
    failing: dict[int, list[str]] = {}
    for case in cases:
        diagnosis = by_number.get(case.number)
        issues = (
            validate_diagnosis(diagnosis, case)
            if diagnosis
            else [f"#{case.number}: no diagnosis was given"]
        )
        if issues:
            failing[case.number] = issues
        elif diagnosis is not None:
            passed.append((case, diagnosis))
    return passed, failing


def _triaged(case: CaseFile, diagnosis: Diagnosis, model: str) -> TriagedPullRequest:
    events = {event.id: event for event in case.events}
    pr = case.pull_request
    return TriagedPullRequest(
        number=case.number,
        title=case.conversation.title,
        url=case.conversation.url,
        author=pr.author,
        author_role=str(case.facts["author_role"]),
        ready_at=pr.ready_at or pr.created_at,
        waited_days=case.waited_days,
        team_responded=case.facts["days_to_first_team_response"] is not None,
        ci_checks=case.conversation.checks,
        merge_conflicts=case.conversation.mergeable == "CONFLICTING",
        lines_changed=case.conversation.additions + case.conversation.deletions,
        diagnosis=diagnosis,
        quotes=[
            QuoteView(
                event=quote.event,
                text=quote.text,
                who=events[quote.event].who,
                role=events[quote.event].role,
                at=events[quote.event].at,
                url=(source.url if (source := events[quote.event].event) else None)
                or case.conversation.url,
            )
            for quote in diagnosis.quotes
        ],
        events_read=len(case.events),
        model=model,
    )


def _cache_key(repo: RepoRef, pr: PullRequest, now: datetime, model: str) -> str:
    """A reading stays valid while the pull request is unchanged, for a day: how long it has
    waited is part of the reading."""
    parts = [PROMPT_VERSION, SYSTEM_PROMPT, model, repo.key, str(pr.number),
             to_iso(pr.updated_at), now.date().isoformat()]  # fmt: skip
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()
