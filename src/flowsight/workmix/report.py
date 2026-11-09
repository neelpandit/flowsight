"""The work mix: model labels joined with statistics computed here, never by the model."""

from collections.abc import Collection, Mapping, Sequence
from statistics import median
from typing import get_args

from flowsight.analytics.metrics import Period, PullRequestFlow, pull_request_flow
from flowsight.domain import PullRequest
from flowsight.workmix.schema import (
    OTHER_AREA,
    Area,
    AreaShare,
    Label,
    LabelledPullRequest,
    SecondLook,
    TypeShare,
    WorkMix,
    WorkType,
)

Rows = list[tuple[PullRequest, PullRequestFlow]]

# Merged without review and at least this large: listed for a second look whatever its label.
SIZE_FLOOR_LINES = 1000
SECOND_LOOK_LIMIT = 10


def build_work_mix(
    merged: Sequence[PullRequest],
    labels: Mapping[int, Label],
    period: Period,
    *,
    areas: Sequence[Area],
    team: Collection[str],
    repository: str,
    not_labelled: list[str],
    models: list[str],
    cached: int,
    prompt_version: str,
) -> WorkMix:
    """`merged` holds the human pull requests merged in `period` and the period before."""
    now = [(pr, pull_request_flow(pr)) for pr in merged if period.contains(pr.merged_at)]
    before = [
        (pr, pull_request_flow(pr)) for pr in merged if period.previous().contains(pr.merged_at)
    ]

    def of_type(rows: Rows, kind: WorkType) -> list[float | None]:
        return [f.wait_for_first_review_hours for pr, f in rows if _type(labels, pr) == kind]

    by_type = [
        TypeShare(
            kind,
            sum(1 for pr, _ in now if _type(labels, pr) == kind),
            sum(1 for pr, _ in before if _type(labels, pr) == kind),
            _median(of_type(now, kind)),
        )
        for kind in get_args(WorkType)
    ]

    def in_area(rows: Rows, name: str) -> Rows:
        return [(pr, f) for pr, f in rows if _area(labels, pr) == name]

    by_area = []
    for area in [*areas, Area(name=OTHER_AREA, description="Everything else")]:
        current, previous = in_area(now, area.name), in_area(before, area.name)
        if not current and not previous:
            continue
        by_area.append(
            AreaShare(
                area=area.name,
                description=area.description,
                current=len(current),
                baseline=len(previous),
                median_wait_hours=_median([f.wait_for_first_review_hours for _, f in current]),
                baseline_median_wait_hours=_median(
                    [f.wait_for_first_review_hours for _, f in previous]
                ),
                community_share=(
                    round(sum(1 for pr, _ in current if pr.author not in team) / len(current), 3)
                    if current
                    else None
                ),
                unreviewed=sum(1 for _, f in current if f.review_count == 0),
            )
        )
    by_area.sort(key=lambda share: -share.current)

    return WorkMix(
        areas=list(areas),
        by_type=[share for share in by_type if share.current or share.baseline],
        by_area=by_area,
        merged=_labelled(now, labels, repository),
        second_look=_second_look(now, labels, repository),
        labelled=sum(1 for pr in merged if pr.number in labels),
        not_labelled=not_labelled,
        models=models,
        cached=cached,
        prompt_version=prompt_version,
    )


def _labelled(
    rows: Rows, labels: Mapping[int, Label], repository: str
) -> list[LabelledPullRequest]:
    """This period's labelled merges, newest first."""
    found = []
    for pr, _ in rows:
        label = labels.get(pr.number)
        if label is None or pr.merged_at is None:
            continue
        url = f"https://github.com/{repository}/pull/{pr.number}"
        found.append(
            LabelledPullRequest(
                pr.number, pr.title, url, pr.merged_at, label.type, label.area, label.risk
            )
        )
    return sorted(found, key=lambda item: item.merged_at, reverse=True)


def _second_look(rows: Rows, labels: Mapping[int, Label], repository: str) -> list[SecondLook]:
    """Merges with no review that the model judged risky, or that are simply large: high risk
    first, then notable, then the largest of the rest."""
    found = []
    for pr, flow in rows:
        label = labels.get(pr.number)
        risky = label is not None and label.risk != "routine"
        if flow.review_count > 0 or not (risky or flow.lines_changed >= SIZE_FLOOR_LINES):
            continue
        reason = (
            label.risk_reason
            if risky and label is not None and label.risk_reason
            else f"Large change ({flow.lines_changed} lines) merged without review"
        )
        found.append(
            SecondLook(
                number=pr.number,
                title=pr.title,
                url=f"https://github.com/{repository}/pull/{pr.number}",
                author=pr.author,
                merged_at=pr.merged_at or pr.updated_at,
                lines_changed=flow.lines_changed,
                type=label.type if label else None,
                area=label.area if label else None,
                risk=label.risk if label else None,
                reason=reason,
                flagged_by="ai" if risky else "size",
            )
        )
    order = {"high": 0, "notable": 1}
    found.sort(key=lambda item: (order.get(item.risk or "", 2), -item.lines_changed))
    return found[:SECOND_LOOK_LIMIT]


def _type(labels: Mapping[int, Label], pr: PullRequest) -> WorkType | None:
    label = labels.get(pr.number)
    return label.type if label else None


def _area(labels: Mapping[int, Label], pr: PullRequest) -> str | None:
    label = labels.get(pr.number)
    return label.area if label else None


def _median(waits: list[float | None]) -> float | None:
    """Median first-review wait of those that got a review (None: never reviewed)."""
    reviewed = [wait for wait in waits if wait is not None]
    return round(median(reviewed), 1) if reviewed else None
