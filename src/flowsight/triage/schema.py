"""What the model must return for each pull request it reads, and what the service returns."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Blocker = Literal[
    "no_team_attention",  # nobody from the team engaged, or the team went quiet
    "waiting_on_author",  # the team asked for changes or information; the author hasn't followed up
    "needs_decision",  # an open question on scope, design or whether to accept it at all
    "failing_checks_or_conflicts",  # CI failures or merge conflicts must be fixed first
    "held_for_release",  # deliberately parked for a release, milestone or freeze
    "blocked_by_other_work",  # waits on another pull request, issue or upstream change
    "slow_review_rounds",  # several review rounds with long gaps between them
    "unclear",
]
WaitingOn = Literal["team", "author", "other"]
Value = Literal["high", "medium", "low"]
Effort = Literal["minutes", "about_an_hour", "deep"]
NextAction = Literal[
    "review_now",  # small, complete and ready: one sitting finishes it
    "prioritise",  # valuable, but needs a real review: schedule it
    "decide",  # maintainers must settle a question first
    "nudge_author",  # the author has to act
    "wait",  # blocked on a release, another pull request or upstream
    "close",  # abandoned, superseded or out of scope
]
Role = Literal["author", "team", "community", "bot"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Quote(_Strict):
    event: str = Field(description='The event ID, such as "e3", or "description".')
    text: str = Field(description="A verbatim excerpt of that event's text, at most 25 words.")


class Diagnosis(_Strict):
    pull_request: int
    summary: str = Field(
        description="What it changes and who benefits, in one sentence, from the description."
    )
    value: Value = Field(description="How much merging it matters to users or the project.")
    review_effort: Effort = Field(description="How long a maintainer needs to review it.")
    blocker: Blocker = Field(description="What held it up the longest.")
    waiting_on: WaitingOn = Field(description="Who has the next move.")
    explanation: str = Field(
        description="One or two sentences on why it waited, grounded in what the thread shows."
    )
    quotes: list[Quote] = Field(description="Up to two excerpts that show it. None for silence.")
    action: NextAction = Field(description="What to do with it now.")
    next_step: str = Field(description="The concrete next step.")
    owner: str | None = Field(
        description="Login of the person best placed to take the next step, from `may_own`."
    )
    reply: str | None = Field(
        description="The comment to post now; null only when it is waiting on something else."
    )


class TriageAnswer(_Strict):
    diagnoses: list[Diagnosis]


# ---- what the service returns: the model's answer joined with verified context ----------


class QuoteView(_Strict):
    event: str
    text: str  # verified verbatim against the conversation
    who: str | None
    role: Role
    at: datetime
    url: str | None


class TriagedPullRequest(_Strict):
    """One pull request as the model read it, with every quote checked against its source."""

    number: int
    title: str  # written by the author: untrusted, render as plain text
    url: str
    author: str | None
    author_role: str
    ready_at: datetime
    waited_days: float  # since it became ready
    # Facts from code, shown beside the reading so the model needn't restate them.
    team_responded: bool
    ci_checks: str | None
    merge_conflicts: bool
    lines_changed: int
    diagnosis: Diagnosis
    quotes: list[QuoteView]
    events_read: int
    model: str
