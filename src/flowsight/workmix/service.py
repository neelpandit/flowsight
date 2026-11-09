import asyncio
import hashlib
import logging
from collections.abc import Sequence

from flowsight.analytics.metrics import Period
from flowsight.domain import PullRequest
from flowsight.insights.llm import AllModelsUnavailableError, InsightModel, with_backups
from flowsight.store import PullRequestStore
from flowsight.triage.casefile import team_members
from flowsight.workmix.prompt import (
    LABEL_PROMPT,
    PROMPT_VERSION,
    TAXONOMY_PROMPT,
    label_message,
    taxonomy_message,
)
from flowsight.workmix.report import build_work_mix
from flowsight.workmix.schema import (
    OTHER_AREA,
    Area,
    Label,
    LabelAnswer,
    TaxonomyAnswer,
    WorkMix,
)

logger = logging.getLogger(__name__)

LABEL_BATCH = 120  # pull requests per model call
PARALLEL_CALLS = 2  # the free tier allows only a few calls a minute
TITLE_LIMIT = 120
TAXONOMY_TITLES = 600
MAX_AREAS = 10
MAX_REASON_WORDS = 25


class WorkMixUnavailableError(Exception):
    """No model could propose the areas, so nothing can be labelled."""


class WorkMixService:
    """Labels every human pull request merged in the period and the one before: its type, its
    area and its risk. The areas are proposed once per repository and then kept, so labels stay
    comparable over time. Labels are checked, cached per pull request, and counted in code."""

    def __init__(self, models: Sequence[InsightModel], store: PullRequestStore) -> None:
        self._models = list(models)
        self._store = store
        self._calls = asyncio.Semaphore(PARALLEL_CALLS)

    async def work_mix(
        self, repo_key: str, repository: str, pull_requests: Sequence[PullRequest], period: Period
    ) -> WorkMix:
        span = Period(period.previous().start, period.end)
        merged = [
            pr for pr in pull_requests if not pr.author_is_bot and span.contains(pr.merged_at)
        ]
        team = set(team_members(pull_requests))
        areas = await self._areas(repo_key, repository, merged)
        fingerprint = _fingerprint(areas)

        labels: dict[int, Label] = {}
        models: set[str] = set()
        for pr in merged:
            for candidate in self._models:
                body = self._store.cached_insight(
                    _label_key(repo_key, pr, candidate.name, fingerprint)
                )
                if body is not None:
                    labels[pr.number] = Label.model_validate_json(body)
                    models.add(candidate.name)
                    break
        cached = len(labels)

        todo = [pr for pr in merged if pr.number not in labels]
        batches = [todo[i : i + LABEL_BATCH] for i in range(0, len(todo), LABEL_BATCH)]
        results = await asyncio.gather(
            *(self._label(repository, areas, batch, team) for batch in batches)
        )
        not_labelled: list[str] = []
        by_number = {pr.number: pr for pr in merged}
        for found, problems, answered_by in results:
            not_labelled += problems
            for label in found:
                labels[label.pull_request] = label
                models.add(answered_by)
                key = _label_key(repo_key, by_number[label.pull_request], answered_by, fingerprint)
                self._store.save_insight(key, answered_by, PROMPT_VERSION, label.model_dump_json())
        return build_work_mix(
            merged, labels, period, areas=areas, team=team, repository=repository,
            not_labelled=not_labelled, models=sorted(models), cached=cached,
            prompt_version=PROMPT_VERSION,
        )  # fmt: skip

    async def _areas(
        self, repo_key: str, repository: str, merged: Sequence[PullRequest]
    ) -> list[Area]:
        """The repository's areas: cached from the first time they were proposed."""
        for model in self._models:
            body = self._store.cached_insight(_taxonomy_key(repo_key, model.name))
            if body is not None:
                return TaxonomyAnswer.model_validate_json(body).areas
        recent = sorted(merged, key=lambda pr: pr.merged_at or pr.updated_at, reverse=True)
        titles = [pr.title[:TITLE_LIMIT] for pr in recent[:TAXONOMY_TITLES]]
        prompt = taxonomy_message(repository, titles)
        try:
            answer, model = await with_backups(
                self._models,
                lambda model: model.generate(TAXONOMY_PROMPT, prompt, TaxonomyAnswer),
            )
        except AllModelsUnavailableError as error:
            raise WorkMixUnavailableError(str(error)) from error
        areas = _clean_areas(answer.areas)
        if len(areas) < 2:
            raise WorkMixUnavailableError(f"{model.name} proposed no usable areas")
        cleaned = TaxonomyAnswer(areas=areas)
        self._store.save_insight(
            _taxonomy_key(repo_key, model.name), model.name, PROMPT_VERSION,
            cleaned.model_dump_json(),
        )  # fmt: skip
        return areas

    async def _label(
        self,
        repository: str,
        areas: list[Area],
        batch: list[PullRequest],
        team: set[str],
    ) -> tuple[list[Label], list[str], str]:
        """Labels that passed the checks, the pull requests left out and why, and the model."""
        try:
            async with self._calls:
                (found, problems), model = await with_backups(
                    self._models, lambda model: self._ask(model, repository, areas, batch, team)
                )
        except AllModelsUnavailableError as error:
            return [], [f"#{pr.number}: {error}" for pr in batch], ""
        return found, problems, model.name

    async def _ask(
        self,
        model: InsightModel,
        repository: str,
        areas: list[Area],
        batch: list[PullRequest],
        team: set[str],
    ) -> tuple[list[Label], list[str]]:
        prompt = label_message(repository, areas, [_item(pr, team) for pr in batch])
        answer = await model.generate(LABEL_PROMPT, prompt, LabelAnswer)
        passed, failing = check_labels(answer, batch, areas)
        if not failing:
            return passed, []
        logger.info("labels failed validation, asking for a repair: %s", failing)
        retry = [pr for pr in batch if pr.number in failing]
        problems = "\n".join(f"- #{n}: {issue}" for n, issue in failing.items())
        repair = (
            f"{label_message(repository, areas, [_item(pr, team) for pr in retry])}\n\n"
            f"A previous answer failed these checks. Label these again and fix them:\n{problems}"
        )
        repaired, still = check_labels(
            await model.generate(LABEL_PROMPT, repair, LabelAnswer), retry, areas
        )
        return passed + repaired, [f"#{n}: {issue}" for n, issue in still.items()]


def check_labels(
    answer: LabelAnswer, batch: Sequence[PullRequest], areas: Sequence[Area]
) -> tuple[list[Label], dict[int, str]]:
    """Labels that pass, with area names made canonical, and the reason each other one fails."""
    names = {area.name.casefold(): area.name for area in areas} | {OTHER_AREA: OTHER_AREA}
    given = {label.pull_request: label for label in answer.labels}
    passed: list[Label] = []
    failing: dict[int, str] = {}
    for pr in batch:
        label = given.get(pr.number)
        if label is None:
            failing[pr.number] = "no label was given"
            continue
        area = names.get(label.area.strip().casefold())
        reason = (label.risk_reason or "").strip()
        if area is None:
            failing[pr.number] = f"area {label.area!r} is not one of the given areas"
        elif label.risk != "routine" and not reason:
            failing[pr.number] = f"{label.risk} risk needs a risk_reason"
        elif len(reason.split()) > MAX_REASON_WORDS or "@" in reason:
            failing[pr.number] = "risk_reason must be a short phrase that mentions no one"
        else:
            kept_reason = reason if label.risk != "routine" else None
            passed.append(label.model_copy(update={"area": area, "risk_reason": kept_reason}))
    return passed, failing


def _item(pr: PullRequest, team: set[str]) -> dict[str, object]:
    return {
        "pull_request": pr.number,
        "untrusted_title": pr.title[:TITLE_LIMIT],
        "lines_added": pr.additions,
        "lines_deleted": pr.deletions,
        "author": "team" if pr.author in team else "community",
    }


def _clean_areas(areas: Sequence[Area]) -> list[Area]:
    """Distinct, non-empty names other than "other", at most MAX_AREAS of them."""
    seen: set[str] = set()
    cleaned = []
    for area in areas:
        name = " ".join(area.name.split())
        if name and name.casefold() not in seen and name.casefold() != OTHER_AREA:
            seen.add(name.casefold())
            cleaned.append(Area(name=name, description=area.description.strip()))
    return cleaned[:MAX_AREAS]


def _fingerprint(areas: Sequence[Area]) -> str:
    return hashlib.sha256("\n".join(area.name for area in areas).encode()).hexdigest()[:16]


def _taxonomy_key(repo_key: str, model: str) -> str:
    parts = ["taxonomy", PROMPT_VERSION, TAXONOMY_PROMPT, model, repo_key]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _label_key(repo_key: str, pr: PullRequest, model: str, fingerprint: str) -> str:
    """A merged pull request's label stays valid while its title and the areas are unchanged."""
    parts = [PROMPT_VERSION, LABEL_PROMPT, model, repo_key, str(pr.number), pr.title, fingerprint]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()
