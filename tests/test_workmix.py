from typing import Any

import pytest
from fastapi.testclient import TestClient

from flowsight.analytics.metrics import Period
from flowsight.api.app import create_app
from flowsight.domain import PullRequest
from flowsight.insights.llm import LlmError
from flowsight.store import PullRequestStore, SyncState, open_database
from flowsight.workmix.report import build_work_mix
from flowsight.workmix.schema import Area, Label, LabelAnswer, TaxonomyAnswer
from flowsight.workmix.service import WorkMixService, WorkMixUnavailableError, check_labels
from tests.fakes import FakeGitHub, ScriptedModel, pull_request, review, utc
from tests.test_api import NOW, settings

PERIOD = Period(utc("2026-10-11T12:00"), utc("2026-11-08T12:00"))
AREAS = [
    Area(name="Routing", description="Navigation and route matching"),
    Area(name="Build", description="The build pipeline"),
]


def merged(number: int, day: str, title: str, **overrides: Any) -> PullRequest:
    fields: dict[str, Any] = {
        "title": title, "author": "dan", "author_association": "NONE",
        "created_at": utc(f"{day}T09:00"), "ready_at": utc(f"{day}T09:00"),
        "merged_at": utc(f"{day}T20:00"), "closed_at": utc(f"{day}T20:00"),
        "updated_at": utc(f"{day}T20:00"), "additions": 40, "deletions": 5,
    }  # fmt: skip
    return pull_request(number, **(fields | overrides))


def approved(day: str, hour: int) -> tuple[Any, ...]:
    return (review("bob", "APPROVED", f"{day}T{hour:02d}:00"),)


PULL_REQUESTS = [
    # This period.
    merged(101, "2026-10-15", "fix(router): keep the scroll position", author="carol",
           reviews=approved("2026-10-15", 13)),
    merged(102, "2026-10-16", "feat: add a --timings flag", author="erin",
           reviews=approved("2026-10-16", 19)),
    # A big rewrite whose title sounds harmless, merged without review.
    merged(103, "2026-10-17", "Tidy up the cache layer", additions=1100, deletions=100),
    merged(104, "2026-10-18", "Change how auth tokens are refreshed"),
    merged(105, "2026-10-19", "Fix a typo in the docs", additions=1, deletions=1),
    # The period before.
    merged(201, "2026-09-20", "fix(router): match trailing slashes", author="carol",
           reviews=approved("2026-09-20", 11)),
    merged(202, "2026-09-21", "fix(router): decode route params", author="erin",
           reviews=approved("2026-09-21", 15)),
]  # fmt: skip


def label(
    number: int, kind: str, area: str, risk: str = "routine", risk_reason: str | None = "-"
) -> Label:
    """A label; risky ones get a stock reason unless the test names one (or None)."""
    reason = (
        None if risk == "routine" else ("Could break things" if risk_reason == "-" else risk_reason)
    )
    return Label.model_validate(
        {"pull_request": number, "type": kind, "area": area, "risk": risk, "risk_reason": reason}
    )


LABELS = [
    label(101, "bug_fix", "Routing", "notable", risk_reason="Changes navigation behaviour"),
    label(102, "feature", "Build"),
    label(103, "refactor", "Build"),  # labelled routine: only its size gets it a second look
    label(104, "bug_fix", "Build", "high", risk_reason="Could expose tokens"),
    label(105, "docs", "other"),
    label(201, "bug_fix", "Routing"),
    label(202, "bug_fix", "Routing"),
]


# ---- the work mix, counted in code ---------------------------------------------------------


def mix() -> Any:
    return build_work_mix(
        PULL_REQUESTS, {item.pull_request: item for item in LABELS}, PERIOD, areas=AREAS,
        team={"bob"}, repository="acme/widgets", not_labelled=[], models=["m"], cached=0,
        prompt_version="v",
    )  # fmt: skip


def test_counts_and_waits_by_type_and_area_come_from_code() -> None:
    work = mix()
    assert {t.type: (t.baseline, t.current) for t in work.by_type} == {
        "feature": (0, 1), "bug_fix": (2, 2), "refactor": (0, 1), "docs": (0, 1),
    }  # fmt: skip
    routing = next(a for a in work.by_area if a.area == "Routing")
    # Waits for a first review: 2 h and 6 h before, 4 h now.
    assert (routing.baseline, routing.current) == (2, 1)
    assert (routing.baseline_median_wait_hours, routing.median_wait_hours) == (4.0, 4.0)
    assert routing.community_share == 1.0
    build = next(a for a in work.by_area if a.area == "Build")
    assert (build.current, build.unreviewed) == (3, 2)


def test_unreviewed_risky_or_large_merges_get_a_second_look() -> None:
    second = mix().second_look
    # #101 is risky but was reviewed; #105 is unreviewed but routine and tiny.
    assert [(s.number, s.flagged_by) for s in second] == [(104, "ai"), (103, "size")]
    assert second[0].reason == "Could expose tokens"
    assert second[1].reason == "Large change (1200 lines) merged without review"


# ---- checks on the labels ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bad", "issue"),
    [
        (label(101, "bug_fix", "Navigation"), "area 'Navigation' is not one of the given areas"),
        (label(101, "bug_fix", "Routing", "high", risk_reason=None), "high risk needs a risk_reason"),
        (label(101, "bug_fix", "Routing", "high", risk_reason="Ask @mallory"), "risk_reason must be a short phrase that mentions no one"),
    ],
)  # fmt: skip
def test_ungrounded_labels_are_rejected(bad: Label, issue: str) -> None:
    passed, failing = check_labels(LabelAnswer(labels=[bad]), PULL_REQUESTS[:1], AREAS)
    assert (passed, failing) == ([], {101: issue})


def test_area_names_are_matched_loosely_and_missing_labels_reported() -> None:
    answer = LabelAnswer(labels=[label(101, "bug_fix", " routing ")])
    passed, failing = check_labels(answer, PULL_REQUESTS[:2], AREAS)
    assert [p.area for p in passed] == ["Routing"]
    assert failing == {102: "no label was given"}


# ---- the service ---------------------------------------------------------------------------


def store() -> PullRequestStore:
    return PullRequestStore(open_database(":memory:"))


async def test_areas_and_labels_are_asked_once_then_cached() -> None:
    model = ScriptedModel(TaxonomyAnswer(areas=AREAS), LabelAnswer(labels=LABELS))
    service = WorkMixService([model], store())
    first = await service.work_mix("acme/widgets", "acme/widgets", PULL_REQUESTS, PERIOD)
    second = await service.work_mix("acme/widgets", "acme/widgets", PULL_REQUESTS, PERIOD)
    assert (first.labelled, first.cached, second.cached) == (7, 0, 7)
    assert len(model.prompts) == 2  # one for the areas, one batch of labels
    assert [s.number for s in second.second_look] == [104, 103]


async def test_failing_labels_are_repaired_or_left_out() -> None:
    wrong = label(104, "bug_fix", "Security", "high", risk_reason="Could expose tokens")
    model = ScriptedModel(
        TaxonomyAnswer(areas=AREAS),
        LabelAnswer(labels=[*LABELS[:3], wrong, *LABELS[4:]]),
        LabelAnswer(labels=[wrong]),
    )
    work = await WorkMixService([model], store()).work_mix(
        "acme/widgets", "acme/widgets", PULL_REQUESTS, PERIOD
    )
    assert work.labelled == 6
    assert work.not_labelled == ["#104: area 'Security' is not one of the given areas"]
    assert "#104: area 'Security'" in model.prompts[-1]
    # Unlabelled, #104 is small, so nothing vouches for a second look.
    assert [s.number for s in work.second_look] == [103]


async def test_a_backup_model_labels_when_the_main_one_is_out_of_quota() -> None:
    main = ScriptedModel(LlmError("RateLimitError: quota exceeded"))
    backup = ScriptedModel(TaxonomyAnswer(areas=AREAS), LabelAnswer(labels=LABELS))
    backup.name = "backup"
    work = await WorkMixService([main, backup], store()).work_mix(
        "acme/widgets", "acme/widgets", PULL_REQUESTS, PERIOD
    )
    assert work.models == ["backup"]


async def test_without_any_model_the_work_mix_is_unavailable() -> None:
    with pytest.raises(WorkMixUnavailableError):
        await WorkMixService([ScriptedModel()], store()).work_mix(
            "acme/widgets", "acme/widgets", PULL_REQUESTS, PERIOD
        )


# ---- the endpoint -------------------------------------------------------------------------


def synced_store() -> PullRequestStore:
    synced = store()
    synced.ensure_repository("acme/widgets", "Acme/Widgets")
    synced.save("acme/widgets", PULL_REQUESTS)
    synced.save_sync_state("acme/widgets", SyncState(utc("2026-09-01T00:00"), NOW))
    return synced


def test_the_endpoint_returns_the_mix_or_a_problem_when_no_model_answers() -> None:
    answers = ScriptedModel(TaxonomyAnswer(areas=AREAS), LabelAnswer(labels=LABELS))
    for model, status in ((answers, 200), (ScriptedModel(), 503)):
        app = create_app(
            settings(), github=FakeGitHub([]), connection=synced_store()._db, model=model,
            clock=lambda: NOW,
        )  # fmt: skip
        with TestClient(app) as client:
            response = client.get("/v1/repos/acme/widgets/work-mix")
        assert response.status_code == status
    assert response.headers["retry-after"] == "60"
