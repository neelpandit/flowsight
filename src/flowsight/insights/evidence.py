"""Turns a flow report, and what was read in pull request conversations, into the numbered facts
the LLM may cite: its whole view of the data."""

from collections import Counter
from collections.abc import Sequence

from flowsight.analytics.metrics import PullRequestFlow, ReviewerSummary
from flowsight.analytics.report import FlowReport
from flowsight.analytics.signals import Signal
from flowsight.insights.schema import EvidenceItem
from flowsight.triage.schema import TriagedPullRequest


def build_evidence(
    report: FlowReport,
    flows: Sequence[PullRequestFlow],
    triaged: Sequence[TriagedPullRequest] = (),
) -> list[EvidenceItem]:
    """Signals (S) and the context needed to explain them (C). Two kinds of fact, both numbers:
    S-items are the tested wait times, C-items the volume, reviewer and queue numbers that can
    explain a wait changing, including counts over the conversations read.

    Statistics reach the model as aggregates; no text written by strangers is included.
    """
    # Decision: the model sees only numbers here. The conversations are read separately
    # (triage), and what reaches this call is counts over those readings ("9 of 15 are waiting
    # on the team"), not the readings themselves. That keeps the facts short and uniform, and
    # keeps strangers' text out of the explanation. Every signal is included, not only detected
    # ones, so the model can see what did *not* change and say so.
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
    reviewers_now, reviewers_before = now.reviewers, before.reviewers
    # Decision: merges, merges without review and reviewer load are context (C-items), never
    # signals: they do not measure waiting, but they are the usual reasons waiting changes. The
    # list is fixed and short: volume in and out, the size of changes, how much reviewing was
    # done and by how many people, the queue, and whether reviews skipped. Each is a plausible
    # cause of a wait changing; anything that is not was left out.
    context: list[tuple[str, str, float | None, float | None]] = [
        ("Human-authored pull requests opened", "pull_requests", now.opened, before.opened),
        ("Human-authored pull requests merged", "pull_requests", now.merged, before.merged),
        (
            "Share of merged pull requests with no review from anyone but the author",
            "ratio",
            now.merged_without_review_rate,
            before.merged_without_review_rate,
        ),
        (
            "Share of all reviews done by the single busiest reviewer",
            "ratio",
            reviewers_now.top_share,
            reviewers_before.top_share,
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
    ]
    # Decision: the busiest reviewers of either period, so someone who went quiet is visible too.
    # "Reviews by X went from 40 to 5" is often the real cause behind a slower first review.
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
    items += [
        EvidenceItem(f"C{n}", label, unit, current, baseline)
        for n, (label, unit, current, baseline) in enumerate(context, start=1)
    ]
    return items


def _reading_totals(
    triaged: Sequence[TriagedPullRequest],
) -> list[tuple[str, str, float | None, float | None]]:
    """Counts over the conversations read, computed here so the model never has to add up."""
    # Decision: LLMs miscount. "9 of 15 are waiting on the team" is counted here and given as a
    # fact, so the model only has to interpret it.
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
    totals.append(
        (
            "Open pull requests read with no response from a maintainer yet",
            "pull_requests",
            sum(1 for item in triaged if not item.team_responded),
            None,
        )
    )
    return totals


def _reviews_by(summary: ReviewerSummary, login: str) -> int | None:
    """A reviewer's count; 0 if the top list is complete and they're absent, else unknown."""
    counts = {load.login: load.reviews for load in summary.top}
    if login in counts:
        return counts[login]
    return 0 if summary.active <= len(summary.top) else None


def _signal_label(signal: Signal) -> str:
    return f"{signal.description} [{signal.category}]"
