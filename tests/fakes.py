"""Test doubles shared across test modules."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from flowsight.domain import PullRequest, Review, ReviewState
from flowsight.github.errors import GitHubUnavailableError
from flowsight.insights.llm import LlmError


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def review(reviewer: str | None, state: ReviewState, at: str, *, bot: bool = False) -> Review:
    return Review(reviewer=reviewer, reviewer_is_bot=bot, state=state, submitted_at=utc(at))


def pull_request(number: int, **overrides: Any) -> PullRequest:
    fields: dict[str, Any] = {
        "number": number,
        "title": f"PR {number}",
        "author": "alice",
        "author_is_bot": False,
        "author_association": "MEMBER",
        "created_at": utc("2026-10-02T10:00"),
        "ready_at": utc("2026-10-02T10:00"),
        "merged_at": utc("2026-10-02T18:00"),
        "closed_at": utc("2026-10-02T18:00"),
        "updated_at": utc("2026-10-02T18:00"),
        "additions": 10,
        "deletions": 2,
        "reviews": (),
    }
    fields.update(overrides)
    return PullRequest(**fields)


class FakeGitHub:
    """An in-memory GitHub that answers the three queries a sync makes, and conversation
    lookups for the pull requests in `conversations`."""

    def __init__(
        self,
        pull_requests: list[dict[str, Any]],
        conversations: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.pull_requests = pull_requests
        self.conversations = conversations or {}
        self.detail_batches: list[list[int]] = []
        self.conversation_batches: list[list[int]] = []
        self.is_private = False
        self.fail_details = False

    async def query[T: BaseModel](
        self, document: str, variables: dict[str, Any], model: type[T]
    ) -> T:
        return model.model_validate(self._respond(document, variables))

    def _respond(self, document: str, variables: dict[str, Any]) -> object:
        if "query Conversations" in document:
            numbers = [variables[f"n{i}"] for i in range(len(variables) - 2)]
            self.conversation_batches.append(numbers)
            return {
                "repository": {
                    f"p{i}": self.conversations.get(number) for i, number in enumerate(numbers)
                }
            }
        if "RepositoryLookup" in document:
            return {"repository": {"nameWithOwner": "acme/widgets", "isPrivate": self.is_private}}
        if "PullRequestPage" in document:
            offset = int(variables["cursor"] or 0)
            by_update = sorted(self.pull_requests, key=lambda pr: pr["updatedAt"], reverse=True)
            page = by_update[offset : offset + 100]
            return {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {
                            "hasNextPage": offset + 100 < len(by_update),
                            "endCursor": str(offset + 100),
                        },
                        "nodes": [{"id": f"PR_{pr['number']}", **pr} for pr in page],
                    }
                }
            }
        if self.fail_details:
            raise GitHubUnavailableError("GitHub returned 502")
        numbers = [int(node_id.removeprefix("PR_")) for node_id in variables["ids"]]
        self.detail_batches.append(numbers)
        by_number = {pr["number"]: pr for pr in self.pull_requests}
        return {"nodes": [detail(by_number[number]) for number in numbers]}


def remote(number: int, created: str, updated: str, closed: str | None = None) -> dict[str, Any]:
    return {"number": number, "createdAt": created, "updatedAt": updated, "closedAt": closed}


def detail(pr: dict[str, Any]) -> dict[str, Any]:
    return {
        **pr,
        "title": f"PR {pr['number']}",
        "isDraft": False,
        "mergedAt": pr["closedAt"],
        "additions": 1,
        "deletions": 1,
        "authorAssociation": "MEMBER",
        "author": {"__typename": "User", "login": "alice"},
        "timelineItems": {"nodes": []},
        "reviews": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }


def comment(
    event_id: str, login: str, at: str, body: str, *, association: str = "NONE", bot: bool = False
) -> dict[str, Any]:
    return {
        "__typename": "IssueComment", "id": event_id, "url": f"https://github.com/c/{event_id}",
        "createdAt": at, "body": body, "authorAssociation": association,
        "author": {"__typename": "Bot" if bot else "User", "login": login},
    }  # fmt: skip


def commit(event_id: str, at: str, headline: str) -> dict[str, Any]:
    return {
        "__typename": "PullRequestCommit", "id": event_id,
        "commit": {"committedDate": at, "messageHeadline": headline},
    }  # fmt: skip


def conversation(number: int, events: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """A pull request conversation as GitHub's GraphQL API returns it."""
    fields: dict[str, Any] = {
        "number": number, "title": f"PR {number}", "url": f"https://github.com/acme/widgets/pull/{number}",
        "state": "OPEN", "isDraft": False, "additions": 30, "deletions": 5, "changedFiles": 2,
        "body": "Fixes the widget.", "mergeable": "MERGEABLE", "authorAssociation": "NONE",
        "author": {"__typename": "User", "login": "carol"},
        "labels": {"nodes": [{"name": "bug"}]},
        "reviewRequests": {"nodes": []},
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        "head": {"totalCount": len(events), "nodes": events[:15]},
        "tail": {"nodes": events[-30:]},
    }  # fmt: skip
    fields.update(overrides)
    return fields


class ScriptedModel:
    """Returns prepared answers. Each call takes the first one that is an exception or matches
    the schema asked for, so one script can serve a pipeline that asks for several kinds. With
    none left for that schema, it behaves like an unavailable model."""

    name = "scripted"

    def __init__(self, *answers: BaseModel | Exception) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    async def generate[T: BaseModel](self, system: str, user: str, schema: type[T]) -> T:
        self.prompts.append(user)
        index = next(
            (i for i, a in enumerate(self.answers) if isinstance(a, Exception | schema)), None
        )
        if index is None:
            raise LlmError(f"no scripted {schema.__name__} left")
        answer = self.answers.pop(index)
        if isinstance(answer, Exception):
            raise answer
        assert isinstance(answer, schema)
        return answer
