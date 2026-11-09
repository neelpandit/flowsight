from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast, get_args

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from flowsight.domain import PullRequest, RepoRef, Review, ReviewState
from flowsight.github.errors import RepositoryNotFoundError


class GraphQLClient(Protocol):
    async def query[T: BaseModel](
        self, document: str, variables: dict[str, Any], model: type[T]
    ) -> T: ...


class Node(BaseModel):
    """GitHub's camelCase fields, read into snake_case attributes."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class Actor(Node):
    typename: str = Field(alias="__typename")
    login: str


# ---- Repository lookup -------------------------------------------------------------------

REPOSITORY_LOOKUP = """
query RepositoryLookup($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { nameWithOwner isPrivate }
}
"""


class RepositoryInfo(Node):
    name_with_owner: str
    is_private: bool


class _RepositoryLookup(Node):
    repository: RepositoryInfo | None


async def lookup_repository(github: GraphQLClient, repo: RepoRef) -> RepositoryInfo | None:
    variables = {"owner": repo.owner, "name": repo.name}
    return (await github.query(REPOSITORY_LOOKUP, variables, _RepositoryLookup)).repository


# ---- Phase 1: a cheap listing, most recently updated first, to find what changed ---------

PULL_REQUEST_PAGE = """
query PullRequestPage($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(first: 100, after: $cursor, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { id number createdAt updatedAt closedAt }
    }
  }
}
"""


class PullRequestSummary(Node):
    id: str
    number: int
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None


class _PageInfo(Node):
    has_next_page: bool
    end_cursor: str | None


class _PullRequestConnection(Node):
    page_info: _PageInfo
    nodes: list[PullRequestSummary | None]


class _RepositoryPullRequests(Node):
    pull_requests: _PullRequestConnection


class _PullRequestPage(Node):
    repository: _RepositoryPullRequests | None


async def list_pull_requests_updated_since(
    github: GraphQLClient, repo: RepoRef, updated_since: datetime
) -> AsyncIterator[PullRequestSummary]:
    """Yields pull requests, most recently updated first, until they predate `updated_since`."""
    cursor: str | None = None
    while True:
        variables = {"owner": repo.owner, "name": repo.name, "cursor": cursor}
        page = await github.query(PULL_REQUEST_PAGE, variables, _PullRequestPage)
        if page.repository is None:
            raise RepositoryNotFoundError(repo)
        connection = page.repository.pull_requests
        for summary in connection.nodes:
            if summary is None:
                continue
            if summary.updated_at < updated_since:
                return
            yield summary
        if not connection.page_info.has_next_page:
            return
        cursor = connection.page_info.end_cursor


# ---- Phase 2: full details, fetched only for the pull requests that need them -----------

PULL_REQUEST_DETAILS = """
query PullRequestDetails($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on PullRequest {
      number title isDraft createdAt updatedAt mergedAt closedAt additions deletions
      authorAssociation
      author { __typename login }
      timelineItems(first: 1, itemTypes: [READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT]) {
        nodes {
          __typename
          ... on ReadyForReviewEvent { createdAt }
          ... on ConvertToDraftEvent { createdAt }
        }
      }
      reviews(first: 100) {
        pageInfo { hasNextPage }
        nodes { state submittedAt author { __typename login } }
      }
    }
  }
}
"""


class _DraftEvent(Node):
    typename: str = Field(alias="__typename")
    created_at: datetime


class _ReviewNode(Node):
    state: str
    submitted_at: datetime | None
    author: Actor | None


class _HasNextPage(Node):
    has_next_page: bool


class _Reviews(Node):
    page_info: _HasNextPage
    nodes: list[_ReviewNode | None]


class _TimelineItems(Node):
    nodes: list[_DraftEvent | None]


class _PullRequestNode(Node):
    number: int
    title: str
    is_draft: bool
    created_at: datetime
    updated_at: datetime
    merged_at: datetime | None
    closed_at: datetime | None
    additions: int
    deletions: int
    author_association: str
    author: Actor | None
    timeline_items: _TimelineItems
    reviews: _Reviews


class _PullRequestDetails(Node):
    nodes: list[_PullRequestNode | None]


@dataclass(frozen=True)
class FetchedPullRequests:
    pull_requests: list[PullRequest]
    # PRs with more than 100 reviews, of which only the first 100 were returned.
    reviews_truncated: list[int]


async def fetch_pull_requests(github: GraphQLClient, ids: Sequence[str]) -> FetchedPullRequests:
    details = await github.query(PULL_REQUEST_DETAILS, {"ids": list(ids)}, _PullRequestDetails)
    found = [node for node in details.nodes if node is not None]
    return FetchedPullRequests(
        pull_requests=[_to_pull_request(node) for node in found],
        reviews_truncated=[node.number for node in found if node.reviews.page_info.has_next_page],
    )


_REVIEW_STATES: frozenset[str] = frozenset(get_args(ReviewState))


def _to_pull_request(node: _PullRequestNode) -> PullRequest:
    reviews = tuple(
        Review(
            reviewer=review.author.login if review.author else None,
            reviewer_is_bot=is_bot(review.author),
            state=cast(ReviewState, review.state),
            submitted_at=review.submitted_at,
        )
        for review in node.reviews.nodes
        # Pending reviews have no submission time and are visible only to their author.
        if review is not None and review.submitted_at is not None and review.state in _REVIEW_STATES
    )
    return PullRequest(
        number=node.number,
        title=node.title,
        author=node.author.login if node.author else None,
        author_is_bot=is_bot(node.author),
        author_association=node.author_association,
        created_at=node.created_at,
        ready_at=_ready_at(node),
        merged_at=node.merged_at,
        closed_at=node.closed_at,
        updated_at=node.updated_at,
        additions=node.additions,
        deletions=node.deletions,
        reviews=reviews,
    )


def _ready_at(node: _PullRequestNode) -> datetime | None:
    """Reviewers can't be expected to start on a draft, so the review clock starts when a PR
    first becomes ready. The earliest draft event tells us how the PR began: a
    ReadyForReviewEvent means it was opened as a draft."""
    first = next((event for event in node.timeline_items.nodes if event is not None), None)
    if first is not None and first.typename == "ReadyForReviewEvent":
        return first.created_at
    if first is None and node.is_draft:
        return None
    return node.created_at


def is_bot(actor: Actor | None) -> bool:
    return actor is not None and (actor.typename == "Bot" or actor.login.endswith("[bot]"))
