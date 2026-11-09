"""Fetches pull request conversations (description, comments, reviews, commits) by number."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from pydantic import Field

from flowsight.domain import (
    Conversation,
    ConversationEvent,
    EventKind,
    PullRequestState,
    RepoRef,
)
from flowsight.github.errors import RepositoryNotFoundError
from flowsight.github.pull_requests import Actor, GraphQLClient, Node, is_bot

# The opening of a long thread says what was asked; its end says where things stand now.
HEAD_EVENTS = 15
TAIL_EVENTS = 30
INLINE_COMMENTS = 5

_ITEM_TYPES = (
    "[ISSUE_COMMENT, PULL_REQUEST_REVIEW, PULL_REQUEST_COMMIT, REVIEW_REQUESTED_EVENT, "
    "READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, LABELED_EVENT]"
)
_FRAGMENTS = f"""
fragment Conversation on PullRequest {{
  number title url state isDraft additions deletions changedFiles body mergeable
  authorAssociation author {{ __typename login }}
  labels(first: 10) {{ nodes {{ name }} }}
  reviewRequests(first: 10) {{
    nodes {{ requestedReviewer {{ __typename ... on User {{ login }} ... on Team {{ name }} }} }}
  }}
  commits(last: 1) {{ nodes {{ commit {{ statusCheckRollup {{ state }} }} }} }}
  head: timelineItems(first: {HEAD_EVENTS}, itemTypes: {_ITEM_TYPES}) {{
    totalCount nodes {{ ...Event }}
  }}
  tail: timelineItems(last: {TAIL_EVENTS}, itemTypes: {_ITEM_TYPES}) {{ nodes {{ ...Event }} }}
}}
fragment Event on PullRequestTimelineItems {{
  __typename
  ... on IssueComment {{
    id url createdAt body authorAssociation author {{ __typename login }}
  }}
  ... on PullRequestReview {{
    id url submittedAt state body authorAssociation author {{ __typename login }}
    comments(first: {INLINE_COMMENTS}) {{ nodes {{ body path }} }}
  }}
  ... on PullRequestCommit {{ id commit {{ committedDate messageHeadline }} }}
  ... on ReviewRequestedEvent {{
    id createdAt
    requestedReviewer {{ __typename ... on User {{ login }} ... on Team {{ name }} }}
  }}
  ... on ReadyForReviewEvent {{ id createdAt }}
  ... on ConvertToDraftEvent {{ id createdAt }}
  ... on LabeledEvent {{ id createdAt label {{ name }} }}
}}
"""


def conversations_query(count: int) -> str:
    """One aliased lookup per pull request. Numbers travel only as Int! variables."""
    variables = ", ".join(f"$n{i}: Int!" for i in range(count))
    lookups = " ".join(
        f"p{i}: pullRequest(number: $n{i}) {{ ...Conversation }}" for i in range(count)
    )
    return (
        f"query Conversations($owner: String!, $name: String!, {variables}) {{\n"
        f"  repository(owner: $owner, name: $name) {{ {lookups} }}\n}}\n{_FRAGMENTS}"
    )


class _Named(Node):
    typename: str = Field(alias="__typename")
    login: str | None = None
    name: str | None = None


class _ReviewRequest(Node):
    requested_reviewer: _Named | None


class _Commit(Node):
    committed_date: datetime
    message_headline: str


class _InlineComment(Node):
    body: str
    path: str


class _InlineComments(Node):
    nodes: list[_InlineComment | None]


class _Label(Node):
    name: str


class _Event(Node):
    """Every timeline item type, read leniently: each sets only its own fields."""

    typename: str = Field(alias="__typename")
    id: str | None = None
    url: str | None = None
    created_at: datetime | None = None
    submitted_at: datetime | None = None
    body: str | None = None
    state: str | None = None
    author_association: str | None = None
    author: Actor | None = None
    comments: _InlineComments | None = None
    commit: _Commit | None = None
    requested_reviewer: _Named | None = None
    label: _Label | None = None


class _Events(Node):
    nodes: list[_Event | None]


class _HeadEvents(_Events):
    total_count: int


class _Nodes[T](Node):
    nodes: list[T | None]


class _CheckRollup(Node):
    state: str


class _CommitStatus(Node):
    status_check_rollup: _CheckRollup | None


class _LastCommit(Node):
    commit: _CommitStatus


class _ConversationNode(Node):
    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    additions: int
    deletions: int
    changed_files: int
    body: str
    mergeable: str
    author_association: str
    author: Actor | None
    labels: _Nodes[_Label]
    review_requests: _Nodes[_ReviewRequest]
    commits: _Nodes[_LastCommit]
    head: _HeadEvents
    tail: _Events


class _Conversations(Node):
    # Keyed by the aliases p0, p1…; each value is validated as a _ConversationNode.
    repository: dict[str, dict[str, Any] | None] | None


async def fetch_conversations(
    github: GraphQLClient, repo: RepoRef, numbers: Sequence[int]
) -> list[Conversation]:
    """Conversations for `numbers`, in that order; pull requests GitHub doesn't return are
    skipped."""
    if not numbers:
        return []
    variables: dict[str, object] = {"owner": repo.owner, "name": repo.name}
    variables |= {f"n{i}": number for i, number in enumerate(numbers)}
    result = await github.query(conversations_query(len(numbers)), variables, _Conversations)
    if result.repository is None:
        raise RepositoryNotFoundError(repo)
    nodes = [result.repository.get(f"p{i}") for i in range(len(numbers))]
    return [
        _to_conversation(_ConversationNode.model_validate(node))
        for node in nodes
        if node is not None
    ]


def _to_conversation(node: _ConversationNode) -> Conversation:
    seen: set[str] = set()
    events = []
    for item in [*node.head.nodes, *node.tail.nodes]:
        event = _to_event(item) if item is not None else None
        if event is not None and event.id not in seen:
            seen.add(event.id)
            events.append(event)
    last_commit = next((c for c in node.commits.nodes if c is not None), None)
    rollup = last_commit.commit.status_check_rollup if last_commit else None
    return Conversation(
        number=node.number,
        title=node.title,
        url=node.url,
        state=cast(PullRequestState, node.state),
        is_draft=node.is_draft,
        author=node.author.login if node.author else None,
        author_association=node.author_association,
        body=node.body,
        labels=tuple(label.name for label in node.labels.nodes if label is not None),
        requested_reviewers=tuple(
            name
            for request in node.review_requests.nodes
            if request is not None and request.requested_reviewer is not None
            for name in [request.requested_reviewer.login or request.requested_reviewer.name]
            if name
        ),
        checks=rollup.state if rollup else None,
        mergeable=node.mergeable,
        additions=node.additions,
        deletions=node.deletions,
        changed_files=node.changed_files,
        events=tuple(sorted(events, key=lambda event: event.at)),
        events_total=node.head.total_count,
    )


_KINDS: dict[str, EventKind] = {
    "IssueComment": "comment",
    "PullRequestReview": "review",
    "PullRequestCommit": "commit",
    "ReviewRequestedEvent": "review_requested",
    "ReadyForReviewEvent": "ready_for_review",
    "ConvertToDraftEvent": "converted_to_draft",
    "LabeledEvent": "labeled",
}


def _to_event(item: _Event) -> ConversationEvent | None:
    kind = _KINDS.get(item.typename)
    at = item.commit.committed_date if item.commit else item.created_at or item.submitted_at
    if kind is None or item.id is None or at is None:
        return None  # an unknown type, or a pending review with no submission time
    text = item.body or ""
    if item.comments:  # a review's inline comments belong to it
        inline = [f"[{c.path}] {c.body}" for c in item.comments.nodes if c is not None]
        text = "\n".join(filter(None, [text, *inline]))
    if item.commit:
        text = item.commit.message_headline
    elif item.label:
        text = item.label.name
    elif item.requested_reviewer:
        text = item.requested_reviewer.login or item.requested_reviewer.name or ""
    return ConversationEvent(
        id=item.id,
        kind=kind,
        at=at,
        actor=item.author.login if item.author else None,
        actor_is_bot=is_bot(item.author),
        actor_association=item.author_association,
        text=text,
        review_state=item.state,
        url=item.url,
    )
