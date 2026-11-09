from collections.abc import Callable
from datetime import timedelta

import pytest

from flowsight.analytics.metrics import Distribution, Period, measure_window, pull_request_flow
from flowsight.analytics.report import build_flow_report
from flowsight.analytics.signals import Signal
from flowsight.domain import PullRequest, Review
from tests.fakes import pull_request, review, utc

OCTOBER = Period(utc("2026-10-01T00:00"), utc("2026-10-29T00:00"))

# Worked example: every number asserted below can be checked by hand.
WORKED_EXAMPLE = [
    # Maintainer PR: reviewed after 2h, approved 4h later, merged 2h after approval.
    pull_request(1, reviews=(review("bob", "COMMENTED", "2026-10-02T12:00"), review("bob", "APPROVED", "2026-10-02T16:00"))),
    # External PR opened as a draft. The author's own review, a bot review and an approval
    # after the merge don't count toward its stages.
    pull_request(
        2, author="carol", author_association="CONTRIBUTOR",
        created_at=utc("2026-10-03T08:00"), ready_at=utc("2026-10-03T20:00"),
        merged_at=utc("2026-10-04T08:00"), closed_at=utc("2026-10-04T08:00"),
        reviews=(
            review("carol", "COMMENTED", "2026-10-03T21:00"),
            review("coderabbitai", "COMMENTED", "2026-10-03T22:00", bot=True),
            review("dave", "APPROVED", "2026-10-04T06:00"),
            review("bob", "APPROVED", "2026-10-04T09:00"),
        ),
    ),
    # Merged with no review at all.
    pull_request(3, author="erin", ready_at=utc("2026-10-05T08:00"), merged_at=utc("2026-10-05T09:00"), closed_at=utc("2026-10-05T09:00")),
    # Bot PR: excluded from velocity and stages, but reviewing it is still review work.
    pull_request(
        4, author="renovate[bot]", author_is_bot=True,
        merged_at=utc("2026-10-06T12:00"), closed_at=utc("2026-10-06T12:00"),
        reviews=(review("bob", "APPROVED", "2026-10-06T11:00"),),
    ),
    # Opened in the window, still open.
    pull_request(5, author="frank", author_association="NONE", created_at=utc("2026-10-10T09:00"), ready_at=utc("2026-10-10T09:00"), merged_at=None, closed_at=None),
    # Merged before the window.
    pull_request(
        6, created_at=utc("2026-09-19T10:00"), ready_at=utc("2026-09-19T10:00"),
        merged_at=utc("2026-09-20T10:00"), closed_at=utc("2026-09-20T10:00"),
        reviews=(review("bob", "APPROVED", "2026-09-20T09:00"),),
    ),
]  # fmt: skip


def test_velocity_counts_human_authored_pull_requests() -> None:
    metrics = measure_window(WORKED_EXAMPLE, OCTOBER).metrics
    assert (metrics.merged, metrics.opened, metrics.bot_merged) == (3, 4, 1)


def test_stages_ignore_self_reviews_bots_and_approvals_after_merge() -> None:
    measurement = measure_window(WORKED_EXAMPLE, OCTOBER)
    external = next(flow for flow in measurement.flows if flow.number == 2)
    assert external.external
    assert (
        external.wait_for_first_review_hours,
        external.review_to_approval_hours,
        external.approval_to_merge_hours,
        external.ready_to_merge_hours,
    ) == (10, 0, 2, 12)
    stages = measurement.metrics.stages
    assert stages.wait_for_first_review == Distribution(2, 6, 8, 9.2)
    assert stages.review_to_approval == Distribution(2, 2, 3, 3.6)
    assert stages.approval_to_merge == Distribution(2, 2, 2, 2)
    assert stages.ready_to_merge == Distribution(3, 8, 10, 11.2)


def test_maintainers_and_external_contributors_are_separated() -> None:
    by_author = measure_window(WORKED_EXAMPLE, OCTOBER).metrics.wait_for_first_review_by_author
    assert by_author.maintainers == Distribution(1, 2, 2, 2)
    assert by_author.external == Distribution(1, 10, 10, 10)


def test_merges_without_review_are_reported() -> None:
    metrics = measure_window(WORKED_EXAMPLE, OCTOBER).metrics
    assert (metrics.merged_without_review, metrics.merged_without_review_rate) == (1, 0.333)


def test_review_concentration() -> None:
    reviewers = measure_window(WORKED_EXAMPLE, OCTOBER).metrics.reviewers
    assert (
        reviewers.active,
        reviewers.reviews,
        reviewers.top_share,
        reviewers.needed_for_half,
    ) == (2, 5, 0.8, 1)
    assert [(r.login, r.reviews, r.share) for r in reviewers.top] == [
        ("bob", 4, 0.8),
        ("dave", 1, 0.2),
    ]


def test_a_review_on_a_draft_counts_as_an_immediate_pickup() -> None:
    pr = pull_request(
        7,
        ready_at=utc("2026-10-02T14:00"),
        reviews=(review("bob", "COMMENTED", "2026-10-02T12:00"),),
    )
    assert pull_request_flow(pr).wait_for_first_review_hours == 0


# ---- signal detection on planted scenarios ---------------------------------------------------

PERIOD = Period(utc("2026-10-29T00:00"), utc("2026-11-26T00:00"))


def window(
    start: Period,
    first_number: int,
    count: int,
    wait_hours: Callable[[int], float],
    reviewer: Callable[[int], str] = lambda i: f"reviewer{i % 4}",
) -> list[PullRequest]:
    """`count` pull requests spread evenly across a 28-day window, each reviewed after `wait_hours`."""
    prs = []
    for i in range(count):
        ready = start.start + timedelta(days=27) * i / count
        reviewed = ready + timedelta(hours=wait_hours(i))
        merged = reviewed + timedelta(hours=1)
        prs.append(
            pull_request(
                first_number + i, author=f"author{i % 3}", created_at=ready, ready_at=ready,
                merged_at=merged, closed_at=merged, updated_at=merged,
                reviews=(Review(reviewer(i), False, "APPROVED", reviewed),),
            )
        )  # fmt: skip
    return prs


def signals(baseline: list[PullRequest], current: list[PullRequest]) -> dict[str, Signal]:
    report = build_flow_report(baseline + current, PERIOD)
    return {signal.metric: signal for signal in report.signals}


def typical(i: int) -> float:
    return 2 + i % 5


BASELINE = PERIOD.previous()


def test_detects_a_planted_slowdown_and_nothing_unrelated() -> None:
    result = signals(
        window(BASELINE, 1, 30, typical), window(PERIOD, 1000, 30, lambda i: 4 * typical(i))
    )
    wait = result["wait_for_first_review_hours"]
    assert (wait.status, wait.current, wait.baseline, wait.change) == ("detected", 16, 4, 3)
    detected = [metric for metric, signal in result.items() if signal.status == "detected"]
    # Every author here is a maintainer, so the maintainer segment moves with the total.
    assert detected == ["wait_for_first_review_hours", "maintainer_wait_for_first_review_hours"]


def test_stays_quiet_when_nothing_changed() -> None:
    result = signals(window(BASELINE, 1, 30, typical), window(PERIOD, 1000, 30, typical))
    assert all(signal.status != "detected" for signal in result.values())


def test_refuses_to_judge_a_handful_of_pull_requests() -> None:
    result = signals(
        window(BASELINE, 1, 4, typical), window(PERIOD, 1000, 4, lambda i: 10 * typical(i))
    )
    assert result["wait_for_first_review_hours"].status == "insufficient_data"


def test_flags_review_work_resting_on_one_person() -> None:
    result = signals(
        window(BASELINE, 1, 30, typical),
        window(PERIOD, 1000, 30, typical, reviewer=lambda i: "bob" if i % 5 == 0 else "alice"),
    )
    concentration = result["top_reviewer_share"]
    assert (concentration.status, concentration.current) == ("detected", 0.8)


@pytest.mark.parametrize(("before", "after"), [(40, 15), (15, 40)])
def test_detects_a_change_in_velocity(before: int, after: int) -> None:
    result = signals(window(BASELINE, 1, before, typical), window(PERIOD, 1000, after, typical))
    velocity = result["merged_pull_requests"]
    assert (velocity.status, velocity.current, velocity.baseline) == ("detected", after, before)


def test_pull_requests_still_waiting_at_the_end_of_a_window_are_counted() -> None:
    window = Period(utc("2026-10-01T00:00"), utc("2026-10-29T00:00"))
    prs = [
        pull_request(1, ready_at=utc("2026-10-05T00:00"), merged_at=None, closed_at=None),
        # Reviewed only after the window ended: still waiting at its end.
        pull_request(2, ready_at=utc("2026-10-05T00:00"), merged_at=None, closed_at=None,
                     reviews=(review("bob", "COMMENTED", "2026-10-30T00:00"),)),
        pull_request(3, ready_at=utc("2026-10-05T00:00"), merged_at=None, closed_at=None,
                     reviews=(review("bob", "COMMENTED", "2026-10-06T00:00"),)),
        pull_request(4, ready_at=utc("2026-10-05T00:00"), merged_at=None,
                     closed_at=utc("2026-10-07T00:00")),  # abandoned
        pull_request(5, ready_at=utc("2026-09-20T00:00"), merged_at=None, closed_at=None),
    ]  # fmt: skip
    assert measure_window(prs, window).metrics.waiting_for_first_review_at_end == 2
