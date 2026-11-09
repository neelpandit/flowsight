from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

import numpy as np

from flowsight.domain import PullRequest, Review

HOUR = timedelta(hours=1)
WEEK = timedelta(weeks=1)
MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


@dataclass(frozen=True, slots=True)
class Period:
    """A half-open interval [start, end)."""

    start: datetime
    end: datetime

    def contains(self, moment: datetime | None) -> bool:
        return moment is not None and self.start <= moment < self.end

    def previous(self) -> "Period":
        """The equally long period immediately before this one."""
        return Period(self.start - (self.end - self.start), self.start)


@dataclass(frozen=True, slots=True)
class PullRequestFlow:
    """One pull request's journey, split into the stages where it can wait."""

    number: int
    title: str
    author: str | None
    external: bool  # authored outside the maintainer team (contributors, first-timers)
    lines_changed: int
    ready_at: datetime | None
    merged_at: datetime | None
    review_count: int
    wait_for_first_review_hours: float | None  # ready → first review by someone else
    review_to_approval_hours: float | None  # first review → first approval
    approval_to_merge_hours: float | None  # last approval before merge → merge
    ready_to_merge_hours: float | None


def _counted_reviews(pr: PullRequest) -> list[Review]:
    """Reviews that represent someone else's attention: bots and authors replying on their own
    pull request don't count. Reviews from since-deleted accounts do."""
    return sorted(
        (
            review
            for review in pr.reviews
            if not review.reviewer_is_bot
            and (review.reviewer is None or review.reviewer != pr.author)
        ),
        key=lambda review: review.submitted_at,
    )


def _hours(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    # A review left while a PR was still a draft counts as an instant pickup, not a negative one.
    return max(0.0, (end - start) / HOUR)


def _at(review: Review | None) -> datetime | None:
    return None if review is None else review.submitted_at


def pull_request_flow(pr: PullRequest) -> PullRequestFlow:
    reviews = _counted_reviews(pr)
    first_review = reviews[0] if reviews else None
    first_approval = next((r for r in reviews if r.state == "APPROVED"), None)
    last_approval = next(
        (
            r
            for r in reversed(reviews)
            if r.state == "APPROVED" and pr.merged_at is not None and r.submitted_at <= pr.merged_at
        ),
        None,
    )
    return PullRequestFlow(
        number=pr.number,
        title=pr.title,
        author=pr.author,
        external=pr.author_association not in MAINTAINER_ASSOCIATIONS,
        lines_changed=pr.additions + pr.deletions,
        ready_at=pr.ready_at,
        merged_at=pr.merged_at,
        review_count=len(reviews),
        wait_for_first_review_hours=_hours(pr.ready_at, _at(first_review)),
        review_to_approval_hours=_hours(
            _at(first_review),
            _at(first_approval),
        ),
        approval_to_merge_hours=_hours(_at(last_approval), pr.merged_at),
        ready_to_merge_hours=_hours(pr.ready_at, pr.merged_at),
    )


@dataclass(frozen=True, slots=True)
class Distribution:
    n: int
    p50: float
    p75: float
    p90: float


def distribution(values: Sequence[float]) -> Distribution | None:
    if not values:
        return None
    p50, p75, p90 = np.quantile(values, [0.5, 0.75, 0.9])  # linear interpolation (R type 7)
    return Distribution(
        len(values), round(float(p50), 1), round(float(p75), 1), round(float(p90), 1)
    )


@dataclass(frozen=True, slots=True)
class StageDistributions:
    wait_for_first_review: Distribution | None
    review_to_approval: Distribution | None
    approval_to_merge: Distribution | None
    ready_to_merge: Distribution | None


@dataclass(frozen=True, slots=True)
class WaitByAuthor:
    maintainers: Distribution | None
    external: Distribution | None


@dataclass(frozen=True, slots=True)
class ReviewerLoad:
    login: str
    reviews: int
    share: float


@dataclass(frozen=True, slots=True)
class ReviewerSummary:
    active: int
    reviews: int
    top_share: float | None  # share of all reviews done by the busiest reviewer
    needed_for_half: int | None  # fewest reviewers doing half of all reviews: a "bus factor"
    top: list[ReviewerLoad]


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    merged: int  # human-authored pull requests merged in the window
    opened: int  # human-authored pull requests opened in the window
    bot_merged: int  # bot pull requests merged (excluded from everything else)
    merged_without_review: int
    merged_without_review_rate: float | None
    median_lines_changed: float | None
    # Became ready in the window and, at its end, still open with no review: the queue that
    # merged-only medians can't see.
    waiting_for_first_review_at_end: int
    stages: StageDistributions
    wait_for_first_review_by_author: WaitByAuthor
    reviewers: ReviewerSummary


@dataclass(frozen=True, slots=True)
class WindowSamples:
    """Raw per-PR values behind a window's metrics, used for significance testing."""

    wait_for_first_review: list[float]
    review_to_approval: list[float]
    approval_to_merge: list[float]
    wait_for_first_review_external: list[float]
    wait_for_first_review_maintainers: list[float]
    reviewed: list[bool]


@dataclass(frozen=True, slots=True)
class WindowMeasurement:
    metrics: WindowMetrics
    samples: WindowSamples
    flows: list[PullRequestFlow]


def _present(values: Sequence[float | None]) -> list[float]:
    return [value for value in values if value is not None]


def measure_window(pull_requests: Sequence[PullRequest], period: Period) -> WindowMeasurement:
    """Measures one window. Velocity and stage times describe PRs *merged* in the window."""
    human = [pr for pr in pull_requests if not pr.author_is_bot]
    flows = [pull_request_flow(pr) for pr in human if period.contains(pr.merged_at)]
    waits = _present([f.wait_for_first_review_hours for f in flows])
    external = _present([f.wait_for_first_review_hours for f in flows if f.external])
    maintainers = _present([f.wait_for_first_review_hours for f in flows if not f.external])
    review_to_approval = _present([f.review_to_approval_hours for f in flows])
    approval_to_merge = _present([f.approval_to_merge_hours for f in flows])
    reviewed = [f.review_count > 0 for f in flows]
    unreviewed = reviewed.count(False)

    metrics = WindowMetrics(
        merged=len(flows),
        opened=sum(1 for pr in human if period.contains(pr.created_at)),
        bot_merged=sum(
            1 for pr in pull_requests if pr.author_is_bot and period.contains(pr.merged_at)
        ),
        merged_without_review=unreviewed,
        merged_without_review_rate=round(unreviewed / len(flows), 3) if flows else None,
        median_lines_changed=median(f.lines_changed for f in flows) if flows else None,
        waiting_for_first_review_at_end=sum(
            1 for pr in human if period.contains(pr.ready_at) and _unreviewed_at(pr, period.end)
        ),
        stages=StageDistributions(
            wait_for_first_review=distribution(waits),
            review_to_approval=distribution(review_to_approval),
            approval_to_merge=distribution(approval_to_merge),
            ready_to_merge=distribution(_present([f.ready_to_merge_hours for f in flows])),
        ),
        wait_for_first_review_by_author=WaitByAuthor(
            distribution(maintainers), distribution(external)
        ),
        reviewers=_reviewer_load(pull_requests, period),
    )
    samples = WindowSamples(
        waits, review_to_approval, approval_to_merge, external, maintainers, reviewed
    )
    return WindowMeasurement(metrics, samples, flows)


def _unreviewed_at(pr: PullRequest, moment: datetime) -> bool:
    """Still open at `moment`, with no review from anyone but the author before it."""
    finished = pr.closed_at is not None and pr.closed_at < moment
    reviewed = any(review.submitted_at < moment for review in _counted_reviews(pr))
    return not finished and not reviewed


def _reviewer_load(pull_requests: Sequence[PullRequest], period: Period) -> ReviewerSummary:
    """Who reviewed in the window, across all pull requests (reviewing bot PRs is still work)."""
    counts = Counter(
        review.reviewer
        for pr in pull_requests
        for review in _counted_reviews(pr)
        if review.reviewer is not None and period.contains(review.submitted_at)
    )
    total = counts.total()
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    covered = needed_for_half = 0
    for _, count in ranked:
        if covered >= total / 2:
            break
        covered += count
        needed_for_half += 1
    return ReviewerSummary(
        active=len(ranked),
        reviews=total,
        top_share=round(ranked[0][1] / total, 3) if total else None,
        needed_for_half=needed_for_half if total else None,
        top=[ReviewerLoad(login, n, round(n / total, 3)) for login, n in ranked[:5]],
    )
