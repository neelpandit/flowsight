"""Turns a flow report, and what was read in pull request conversations, into the numbered facts
the LLM may cite: its whole view of the data."""

from collections import Counter
from collections.abc import Sequence

from flowsight.analytics.metrics import PullRequestFlow, ReviewerSummary
from flowsight.analytics.report import FlowReport
from flowsight.analytics.signals import CONCENTRATION_SHARE, Signal
from flowsight.insights.schema import EvidenceItem
from flowsight.triage.schema import TriagedPullRequest
from flowsight.workmix.schema import WorkMix

TITLE_LIMIT = 80
SLOWEST_EXAMPLES = 3
AREAS_SHOWN = 6


def build_evidence(
    report: FlowReport,
    flows: Sequence[PullRequestFlow],
    triaged: Sequence[TriagedPullRequest] = (),
    work_mix: WorkMix | None = None,
) -> list[EvidenceItem]:
    """Signals (S), the context needed to explain them (C), and either one reading per pull
    request conversation (T) or, without those, a few slow examples (U).

    Statistics reach the model as aggregates. Text written by others (titles, and the readings
    of their conversations) is truncated or checked first, and marked as untrusted.
    """
    items = [
        EvidenceItem(
            id=signal.id,
            label=_signal_label(signal),
            unit=signal.unit,
            current=signal.current,
            baseline=signal.baseline,
            change=signal.change,
            status=signal.status,
            q_value=signal.q_value,
            sample_size=(signal.sample_size.current, signal.sample_size.baseline),
        )
        for signal in report.signals
    ]

    now, before = report.current, report.baseline
    days = (report.period.end - report.period.start).days
    reviewers_now, reviewers_before = now.reviewers, before.reviewers
    context: list[tuple[str, str, float | None, float | None]] = [
        ("Days in each period", "days", days, days),
        ("Human-authored pull requests opened", "pull_requests", now.opened, before.opened),
        (
            "Bot-authored pull requests merged (excluded elsewhere)",
            "pull_requests",
            now.bot_merged,
            before.bot_merged,
        ),
        (
            "Median lines changed per merged pull request",
            "lines",
            now.median_lines_changed,
            before.median_lines_changed,
        ),
        ("Reviews submitted", "reviews", reviewers_now.reviews, reviewers_before.reviews),
        ("Active reviewers", "people", reviewers_now.active, reviewers_before.active),
        (
            "Pull requests that became ready in the period and, at its end, had no review yet",
            "pull_requests",
            now.waiting_for_first_review_at_end,
            before.waiting_for_first_review_at_end,
        ),
        (
            "Fewest reviewers who did half of all reviews",
            "people",
            reviewers_now.needed_for_half,
            reviewers_before.needed_for_half,
        ),
    ]
    # The busiest reviewers of either period, so someone who went quiet is visible too.
    logins = dict.fromkeys(
        load.login for load in [*now.reviewers.top[:3], *before.reviewers.top[:3]]
    )
    context += [
        (
            f"Reviews by {login}",
            "reviews",
            _reviews_by(reviewers_now, login),
            _reviews_by(reviewers_before, login),
        )
        for login in logins
    ]
    context += _reading_totals(triaged)
    if work_mix is not None:
        context += _work_mix_context(work_mix)
    items += [
        EvidenceItem(f"C{n}", label, unit, current, baseline)
        for n, (label, unit, current, baseline) in enumerate(context, start=1)
    ]
    if triaged:
        return items + [_reading(f"T{n}", item) for n, item in enumerate(triaged, start=1)]

    slowest = sorted(
        (flow for flow in flows if flow.wait_for_first_review_hours is not None),
        key=lambda flow: -(flow.wait_for_first_review_hours or 0),
    )[:SLOWEST_EXAMPLES]
    items += [
        EvidenceItem(
            id=f"U{n}",
            label="Example slow pull request: hours from ready-for-review to first review",
            unit="hours",
            current=round(flow.wait_for_first_review_hours or 0, 1),
            baseline=None,
            pull_request=flow.number,
            lines_changed=flow.lines_changed,
            untrusted_title=flow.title[:TITLE_LIMIT],
        )
        for n, flow in enumerate(slowest, start=1)
    ]
    return items


def _work_mix_context(work_mix: WorkMix) -> list[tuple[str, str, float | None, float | None]]:
    """Merged work by type and area: labels from a model, every count and median from code."""
    rows: list[tuple[str, str, float | None, float | None]] = [
        (f"Merged pull requests of type: {share.type}", "pull_requests", share.current,
         share.baseline)
        for share in work_mix.by_type
    ]  # fmt: skip
    for area in work_mix.by_area[:AREAS_SHOWN]:
        name = f"Area '{area.area}'"
        rows += [
            (f"{name}: merged pull requests", "pull_requests", area.current, area.baseline),
            (f"{name}: median hours to first review", "hours", area.median_wait_hours,
             area.baseline_median_wait_hours),
            (f"{name}: share of merges by community authors", "ratio", area.community_share,
             None),
        ]  # fmt: skip
    rows.append(
        (
            "Merged this period without review and flagged for a second look",
            "pull_requests",
            len(work_mix.second_look),
            None,
        )
    )
    return rows


def _reading_totals(
    triaged: Sequence[TriagedPullRequest],
) -> list[tuple[str, str, float | None, float | None]]:
    """Counts over the conversations read, computed here so the model never has to add up."""
    if not triaged:
        return []
    totals: list[tuple[str, str, float | None, float | None]] = [
        ("Open pull requests whose conversations were read", "pull_requests", len(triaged), None)
    ]
    next_move = Counter(item.diagnosis.waiting_on for item in triaged)
    totals += [
        (f"Open pull requests read where the next move is with: {who}", "pull_requests", n, None)
        for who, n in next_move.most_common()
    ]
    actions = Counter(item.diagnosis.action for item in triaged)
    totals += [
        (f"Open pull requests read, recommended action: {action}", "pull_requests", n, None)
        for action, n in actions.most_common()
    ]
    totals.append(
        (
            "Open pull requests read with no response from a maintainer yet",
            "pull_requests",
            sum(1 for item in triaged if not item.team_responded),
            None,
        )
    )
    blockers = Counter(item.diagnosis.blocker for item in triaged)
    totals += [
        (f"Pull requests read, held up mainly by: {blocker}", "pull_requests", n, None)
        for blocker, n in blockers.most_common()
    ]
    return totals


def _reading(evidence_id: str, item: TriagedPullRequest) -> EvidenceItem:
    d = item.diagnosis
    quotes = " ".join(f'Quote ({q.who}, {q.role}): "{q.text}"' for q in item.quotes)
    owner = f" Owner: {d.owner}." if d.owner else ""
    return EvidenceItem(
        id=evidence_id,
        label=(
            f"Open pull request, {item.author_role} author ({item.author}); "
            f"value: {d.value}; held up by: {d.blocker}; next move with: {d.waiting_on}; "
            f"action: {d.action}. Days waited"
        ),
        unit="days",
        current=item.waited_days,
        baseline=None,
        pull_request=item.number,
        untrusted_title=item.title[:TITLE_LIMIT],
        detail=f"{d.summary} {d.explanation} Next step: {d.next_step}{owner} {quotes}".strip(),
    )


def _reviews_by(summary: ReviewerSummary, login: str) -> int | None:
    """A reviewer's count; 0 if the top list is complete and they're absent, else unknown."""
    counts = {load.login: load.reviews for load in summary.top}
    if login in counts:
        return counts[login]
    return 0 if summary.active <= len(summary.top) else None


def _signal_label(signal: Signal) -> str:
    label = f"{signal.description} [{signal.category}]"
    if signal.test == "threshold":
        # Not a before/after test: "detected" means a standing risk in the current period.
        return f"{label}; detected means at or above {CONCENTRATION_SHARE:.0%} this period"
    return label
