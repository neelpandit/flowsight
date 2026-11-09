"""What the model returns when labelling merged work, and the work mix built from the labels."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

WorkType = Literal[
    "feature", "bug_fix", "performance", "refactor", "docs", "tests_and_ci", "dependencies",
    "chore",
]  # fmt: skip
Risk = Literal["routine", "notable", "high"]
OTHER_AREA = "other"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Area(_Strict):
    name: str = Field(description="Two or three words, as the team would say it.")
    description: str = Field(description="What belongs here, in one short sentence.")


class TaxonomyAnswer(_Strict):
    areas: list[Area] = Field(description="Five to ten areas that split the work usefully.")


class Label(_Strict):
    pull_request: int
    type: WorkType
    area: str = Field(description='One of the given area names, or "other".')
    risk: Risk = Field(description="How much could break if this change were wrong.")
    risk_reason: str | None = Field(
        description="For notable or high risk: what could break, in a short phrase. Else null."
    )


class LabelAnswer(_Strict):
    labels: list[Label]


# ---- the work mix: labels joined with statistics computed in code ---------------------------


@dataclass(frozen=True, slots=True)
class TypeShare:
    type: WorkType
    current: int  # merged in the period
    baseline: int  # merged in the period before
    median_wait_hours: float | None  # first-review wait, this period


@dataclass(frozen=True, slots=True)
class AreaShare:
    area: str
    description: str
    current: int
    baseline: int
    median_wait_hours: float | None
    baseline_median_wait_hours: float | None
    community_share: float | None  # share of this period's merges written outside the team
    unreviewed: int  # merged this period without review


@dataclass(frozen=True, slots=True)
class LabelledPullRequest:
    number: int
    title: str  # untrusted
    url: str
    merged_at: datetime
    type: WorkType
    area: str
    risk: Risk


@dataclass(frozen=True, slots=True)
class SecondLook:
    """A merge with no review that deserves one after the fact."""

    number: int
    title: str  # untrusted
    url: str
    author: str | None
    merged_at: datetime
    lines_changed: int
    type: WorkType | None  # None when the model gave no valid label
    area: str | None
    risk: Risk | None
    reason: str
    # "ai": the model judged it notable or high risk; "size": large enough that code lists it
    # whatever the label says, since a title written to look harmless can't talk it off the list.
    flagged_by: Literal["ai", "size"]


@dataclass(frozen=True, slots=True)
class WorkMix:
    areas: list[Area]
    by_type: list[TypeShare]
    by_area: list[AreaShare]
    merged: list[LabelledPullRequest]  # this period's merges, newest first
    second_look: list[SecondLook]
    labelled: int
    not_labelled: list[str]  # pull requests without a label, and why
    models: list[str]
    cached: int
    prompt_version: str
