from typing import Any

import pytest
from pydantic import BaseModel

from flowsight.domain import PullRequest
from flowsight.github.pull_requests import fetch_pull_requests
from tests.fakes import utc


class Answering:
    """Answers every query with `data`, validated by the caller's model like the real client."""

    def __init__(self, data: object) -> None:
        self.data = data

    async def query[T: BaseModel](
        self, document: str, variables: dict[str, Any], model: type[T]
    ) -> T:
        return model.model_validate(self.data)


def node(**overrides: Any) -> dict[str, Any]:
    return {
        "number": 7,
        "title": "Fix typo",
        "isDraft": False,
        "createdAt": "2026-10-01T10:00:00Z",
        "updatedAt": "2026-10-02T10:00:00Z",
        "mergedAt": "2026-10-02T10:00:00Z",
        "closedAt": "2026-10-02T10:00:00Z",
        "additions": 10,
        "deletions": 2,
        "authorAssociation": "CONTRIBUTOR",
        "author": {"__typename": "User", "login": "alice"},
        "timelineItems": {"nodes": []},
        "reviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    } | overrides


async def mapped(**overrides: Any) -> PullRequest:
    fetched = await fetch_pull_requests(Answering({"nodes": [node(**overrides)]}), ["PR_7"])
    return fetched.pull_requests[0]


def draft_event(kind: str, at: str) -> dict[str, Any]:
    return {"timelineItems": {"nodes": [{"__typename": kind, "createdAt": at}]}}


async def test_review_clock_starts_at_creation_for_prs_opened_ready() -> None:
    pr = await mapped()
    assert pr.ready_at == pr.created_at == utc("2026-10-01T10:00")


async def test_review_clock_starts_when_a_draft_is_marked_ready() -> None:
    pr = await mapped(**draft_event("ReadyForReviewEvent", "2026-10-01T15:00:00Z"))
    assert pr.ready_at == utc("2026-10-01T15:00")


async def test_pr_opened_ready_then_converted_to_draft_keeps_its_creation_time() -> None:
    pr = await mapped(isDraft=True, **draft_event("ConvertToDraftEvent", "2026-10-01T12:00:00Z"))
    assert pr.ready_at == utc("2026-10-01T10:00")


async def test_pr_still_in_its_original_draft_has_no_ready_time() -> None:
    pr = await mapped(isDraft=True, mergedAt=None, closedAt=None)
    assert pr.ready_at is None


@pytest.mark.parametrize(
    ("author", "is_bot"),
    [
        ({"__typename": "Bot", "login": "dependabot"}, True),
        ({"__typename": "User", "login": "renovate[bot]"}, True),
        ({"__typename": "User", "login": "alice"}, False),
        (None, False),
    ],
)
async def test_bot_authors_are_recognised(author: dict[str, str] | None, is_bot: bool) -> None:
    pr = await mapped(author=author)
    assert pr.author_is_bot is is_bot
    assert pr.author == (author["login"] if author else None)


async def test_submitted_reviews_are_kept_and_pending_ones_dropped() -> None:
    pr = await mapped(
        reviews={
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {"state": "APPROVED", "submittedAt": "2026-10-02T09:00:00Z", "author": {"__typename": "User", "login": "bob"}},
                {"state": "COMMENTED", "submittedAt": "2026-10-01T11:00:00Z", "author": None},
                {"state": "PENDING", "submittedAt": None, "author": {"__typename": "User", "login": "carol"}},
            ],
        }
    )  # fmt: skip
    assert [(r.reviewer, r.state) for r in pr.reviews] == [("bob", "APPROVED"), (None, "COMMENTED")]


async def test_reports_truncated_reviews_and_skips_missing_nodes() -> None:
    github = Answering(
        {"nodes": [node(reviews={"pageInfo": {"hasNextPage": True}, "nodes": []}), None]}
    )
    fetched = await fetch_pull_requests(github, ["PR_7", "PR_gone"])
    assert len(fetched.pull_requests) == 1
    assert fetched.reviews_truncated == [7]
