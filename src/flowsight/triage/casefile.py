"""Chooses which pull requests to read, and turns each conversation into a case file: the facts
code can establish, plus the thread itself, trimmed and marked as untrusted text."""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from flowsight.analytics.metrics import MAINTAINER_ASSOCIATIONS, Period
from flowsight.domain import Conversation, ConversationEvent, PullRequest
from flowsight.triage.schema import Role

OPEN_LIMIT = 15  # the queue a maintainer could clear in a week
MIN_WAIT = timedelta(days=2)  # younger pull requests aren't stuck yet
TEAM_MIN_APPROVALS = 2
TEAM_SHOWN = 8  # the most active team reviewers, offered as owners
TEXT_LIMIT = 500
BOT_TEXT_LIMIT = 160
DESCRIPTION_LIMIT = 800
DAY = timedelta(days=1)


def pick_pull_requests(
    pull_requests: Sequence[PullRequest], period: Period, now: datetime
) -> list[PullRequest]:
    """The longest-waiting pull requests that became ready in the period and are still open."""
    return sorted(
        (
            pr
            for pr in pull_requests
            if not pr.author_is_bot
            and pr.merged_at is None
            and pr.closed_at is None
            and period.contains(pr.ready_at)
            and pr.ready_at is not None
            and min(now, period.end) - pr.ready_at >= MIN_WAIT
        ),
        key=lambda pr: pr.ready_at or now,
    )[:OPEN_LIMIT]


def team_members(pull_requests: Sequence[PullRequest]) -> list[str]:
    """People who approved at least two merged pull requests written by someone else, busiest
    first. GitHub's own author association hides members whose membership is private."""
    approvals = Counter(
        review.reviewer
        for pr in pull_requests
        if pr.merged_at is not None
        for review in {r.reviewer: r for r in pr.reviews if r.state == "APPROVED"}.values()
        if review.reviewer and not review.reviewer_is_bot and review.reviewer != pr.author
    )
    return [login for login, count in approvals.most_common() if count >= TEAM_MIN_APPROVALS]


@dataclass(frozen=True, slots=True)
class CaseEvent:
    id: str  # e1, e2… in time order; "description" is the pull request body
    event: ConversationEvent | None  # None for the description and runs of pushes
    at: datetime
    who: str | None
    role: Role
    kind: str
    text: str  # exactly what the model sees, so quotes can be checked against it


@dataclass(frozen=True, slots=True)
class CaseFile:
    pull_request: PullRequest
    conversation: Conversation
    facts: dict[str, object]
    events: list[CaseEvent]
    team: list[str]  # who may be named as the owner of a next step, with the author

    @property
    def number(self) -> int:
        return self.conversation.number

    @property
    def waited_days(self) -> float:
        value = self.facts.get("days_since_ready")
        return value if isinstance(value, float) else 0.0

    def owners(self) -> set[str]:
        return {
            *self.team,
            *self.conversation.requested_reviewers,
            *([self.conversation.author] if self.conversation.author else []),
        }

    def quotable(self) -> dict[str, str]:
        return {event.id: event.text for event in self.events}


def build_case_file(
    pr: PullRequest, conversation: Conversation, team: Sequence[str], now: datetime
) -> CaseFile:
    ready = pr.ready_at or pr.created_at
    team_set = set(team)

    def role(event: ConversationEvent) -> Role:
        if event.actor_is_bot:
            return "bot"
        if event.kind == "commit" or (event.actor is not None and event.actor == pr.author):
            return "author"
        if event.actor in team_set or event.actor_association in MAINTAINER_ASSOCIATIONS:
            return "team"
        return "community"

    events = [
        CaseEvent(
            "description", None, pr.created_at, pr.author, "author", "description",
            _clean(conversation.body, DESCRIPTION_LIMIT),
        )
    ]  # fmt: skip
    commits: list[ConversationEvent] = []
    # Labels are already a fact; dropping them first keeps a run of pushes together.
    for event in [*(e for e in conversation.events if e.kind != "labeled"), None]:
        if event is not None and event.kind == "commit":
            commits.append(event)
            continue
        if commits:  # a run of pushes reads as one event
            last = commits[-1]
            summary = f"{len(commits)} commit(s) pushed; latest: {_clean(last.text, 120)}"
            events.append(CaseEvent("", None, last.at, pr.author, "author", "commits", summary))
            commits = []
        if event is not None:
            who_role = role(event)
            limit = BOT_TEXT_LIMIT if who_role == "bot" else TEXT_LIMIT
            text = _clean(event.text, limit)
            if event.kind == "review" and event.review_state:
                text = f"[{event.review_state}] {text}".strip()
            events.append(CaseEvent("", event, event.at, event.actor, who_role, event.kind, text))
    events = [events[0]] + [
        CaseEvent(f"e{i}", e.event, e.at, e.who, e.role, e.kind, e.text)
        for i, e in enumerate(events[1:], start=1)
    ]

    def days(start: datetime, stop: datetime) -> float:
        return round(max(0.0, (stop - start) / DAY), 1)

    first_team = next((e for e in events[1:] if e.at >= ready and e.role == "team"), None)
    people = [e for e in events[1:] if e.role != "bot"]
    facts: dict[str, object] = {
        "author": pr.author,
        "author_role": "team" if pr.author in team_set else "community",
        "ready_for_review": ready.date().isoformat(),
        "lines_changed": conversation.additions + conversation.deletions,
        "files_changed": conversation.changed_files,
        "labels": list(conversation.labels),
        "ci_checks": conversation.checks or "none",
        "review_requested_from": list(conversation.requested_reviewers),
        "days_to_first_team_response": days(ready, first_team.at) if first_team else None,
        "days_since_ready": days(ready, now),
        "merge_conflicts": conversation.mergeable == "CONFLICTING",
        "last_to_act": f"{people[-1].who} ({people[-1].role})" if people else None,
    }
    for who in ("team", "author"):
        latest = next((e for e in reversed(events[1:]) if e.role == who), None)
        facts[f"days_since_last_{who}_activity"] = days(latest.at, now) if latest else None
    shown = len(conversation.events)
    if conversation.events_total > shown:
        facts["events_not_shown"] = conversation.events_total - shown
    return CaseFile(pr, conversation, facts, events, list(team[:TEAM_SHOWN]))


_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SPACE = re.compile(r"\s+")


def _clean(text: str, limit: int) -> str:
    """Hidden HTML comments are dropped (templates, and a place to hide instructions);
    whitespace is collapsed and the text truncated on a word boundary."""
    text = _SPACE.sub(" ", _HTML_COMMENT.sub(" ", text)).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"
