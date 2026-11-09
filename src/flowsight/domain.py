import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

# GitHub naming rules: an owner is 1-39 alphanumerics with single inner hyphens; a repository
# name is 1-100 of [A-Za-z0-9._-] and cannot be "." or "..". Anything user-supplied that names
# a repository is checked against these rules before it reaches GitHub or the database.
_OWNER = re.compile(r"(?=.{1,39}\Z)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*\Z")
_NAME = re.compile(r"(?!\.{1,2}\Z)[A-Za-z0-9._-]{1,100}\Z")


@dataclass(frozen=True, slots=True)
class RepoRef:
    owner: str
    name: str

    @property
    def key(self) -> str:
        """Case-insensitive identity, matching how GitHub resolves owners and names."""
        return f"{self.owner}/{self.name}".lower()


def parse_repo_ref(owner: str, name: str) -> RepoRef | None:
    if _OWNER.match(owner) and _NAME.match(name):
        return RepoRef(owner, name)
    return None


def parse_repo_slug(slug: str) -> RepoRef | None:
    """Parses `owner/name`."""
    owner, separator, name = slug.partition("/")
    return parse_repo_ref(owner, name) if separator else None


ReviewState = Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"]


@dataclass(frozen=True, slots=True)
class Review:
    reviewer: str | None  # None when the account has since been deleted
    reviewer_is_bot: bool
    state: ReviewState
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A pull request as stored and analysed. All datetimes are timezone-aware UTC."""

    number: int
    title: str
    author: str | None  # None when the account has since been deleted
    author_is_bot: bool
    author_association: str  # e.g. MEMBER, CONTRIBUTOR, NONE
    created_at: datetime
    ready_at: datetime | None  # when it first became reviewable; None while still a draft
    merged_at: datetime | None
    closed_at: datetime | None
    updated_at: datetime
    additions: int
    deletions: int
    reviews: tuple[Review, ...] = ()


def to_iso(moment: datetime) -> str:
    """Fixed-width UTC ISO-8601 (millisecond precision), so stored values sort as strings."""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


PullRequestState = Literal["OPEN", "CLOSED", "MERGED"]
EventKind = Literal[
    "comment", "review", "commit", "review_requested", "ready_for_review", "converted_to_draft",
    "labeled",
]  # fmt: skip


@dataclass(frozen=True, slots=True)
class ConversationEvent:
    """One thing that happened on a pull request. `text` is written by whoever acted: untrusted."""

    id: str
    kind: EventKind
    at: datetime
    actor: str | None  # None for system events and deleted accounts
    actor_is_bot: bool
    actor_association: str | None
    text: str  # comment or review body, commit headline, label name, or requested reviewer
    review_state: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    """A pull request's discussion as GitHub shows it now, for reading why it waited."""

    number: int
    title: str
    url: str
    state: PullRequestState
    is_draft: bool
    author: str | None
    author_association: str
    body: str
    labels: tuple[str, ...]
    requested_reviewers: tuple[str, ...]
    checks: str | None  # combined CI state of the latest commit: SUCCESS, FAILURE, PENDING…
    mergeable: str  # MERGEABLE, CONFLICTING or UNKNOWN
    additions: int
    deletions: int
    changed_files: int
    events: tuple[ConversationEvent, ...]  # oldest first
    events_total: int  # events on GitHub; long threads keep only the start and the end
