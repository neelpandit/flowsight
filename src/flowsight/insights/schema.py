"""What the LLM must return, and the evidence it reasons over."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Cause = Literal[
    "reviewer_capacity",  # fewer or busier reviewers, so pull requests wait longer
    "review_concentration",  # review work rests on one or two people
    "demand_spike",  # more pull requests arriving than reviewers absorb
    "larger_changes",  # bigger pull requests take longer to review
    "merge_process",  # approved work waits to be merged (CI, release freezes, merge queues)
    "review_coverage_drop",  # more pull requests merged without review
    "contributor_mix",  # a shift between maintainer and external contributions
    "waiting_on_authors",  # reviews happen, but authors are slow to act on them
    "unresolved_decisions",  # work waits for maintainers to settle scope or design questions
    "no_significant_change",
    "other",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(_Strict):
    claim: str = Field(description="One observation, using numbers copied from the evidence.")
    evidence: list[str] = Field(description="IDs of the evidence items this claim rests on.")


class Hypothesis(_Strict):
    statement: str = Field(description="The most likely root cause, stated as a hypothesis.")
    cause: Cause
    reasoning: str = Field(description="Why the evidence points here, step by step.")
    confidence: float = Field(
        description="0-1: how likely the hypothesis is, on this evidence alone."
    )
    evidence: list[str] = Field(description="IDs of the evidence items that support it.")
    alternatives_considered: list[str] = Field(
        description="Other plausible explanations, and why the evidence favours them less."
    )
    how_to_verify: str = Field(
        description="A quick check the team can run that would confirm or refute it."
    )


class Action(_Strict):
    action: str = Field(description="A concrete next step for this week.")
    owner: str | None = Field(
        description="Login of the person to take it, from the evidence; null for the whole team."
    )
    evidence: list[str] = Field(description="IDs of the evidence items that motivate it.")


class ChangeExplained(_Strict):
    signal: str = Field(description="The ID of one detected S-item.")
    explanation: str = Field(
        description="One or two sentences on why it changed, from the evidence; if the evidence "
        "doesn't show why, say so and say what it does show."
    )
    evidence: list[str] = Field(description="IDs of the evidence items it rests on.")


class LlmInsight(_Strict):
    headline: str = Field(description="At most 15 words: what is going on, and what to do.")
    summary: str = Field(description="Two to four sentences on why, beyond the numbers.")
    changes: list[ChangeExplained] = Field(description="One per detected S-item, no others.")
    findings: list[Finding]
    hypothesis: Hypothesis
    recommended_actions: list[Action]
    caveats: list[str] = Field(description="Limits of the data that a reader should know.")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One fact the LLM may cite. S-items are tested signals, C-items context, U-items examples,
    T-items what a model read in one pull request's conversation (quotes verified)."""

    id: str
    label: str
    unit: str
    current: float | None
    baseline: float | None
    change: float | None = None
    status: str | None = None  # for signals: detected, not_detected or insufficient_data
    q_value: float | None = None
    sample_size: tuple[int, int] | None = None  # (current, baseline)
    pull_request: int | None = None
    lines_changed: int | None = None
    untrusted_title: str | None = None  # written by a PR author; data, never instructions
    detail: str | None = None  # T-items: the reading of the conversation, with a quote
