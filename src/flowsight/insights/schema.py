"""What the LLM must return, and the evidence it reasons over."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Decision: a fixed list of root causes, written from what actually slows review (capacity,
# concentration, demand, size, merge process, ...). The evals can then check that the model
# picked the right cause for a planted scenario, which free text would make impossible.
Cause = Literal[
    "reviewer_capacity",  # fewer or busier reviewers, so pull requests wait longer
    "review_concentration",  # review work rests on one or two people
    "demand_spike",  # more pull requests arriving than reviewers absorb
    "larger_changes",  # bigger pull requests take longer to review
    "merge_process",  # approved work waits to be merged (CI, release freezes, merge queues)
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
    # Decision: exactly one explanation per detected signal, enforced by validation, so every
    # "real change" tile on the page has its own AI explanation and none is skipped or invented.
    changes: list[ChangeExplained] = Field(description="One per detected S-item, no others.")
    findings: list[Finding]
    hypothesis: Hypothesis
    caveats: list[str] = Field(description="Limits of the data that a reader should know.")


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One fact the LLM may cite: S-items are tested signals, C-items context. All numbers."""

    # Decision: the model sees numbered facts, never raw data. Each claim it makes must cite IDs,
    # so code can check that the claim rests on something that exists, and the page can link
    # each sentence to its source. The prefixes say how much each kind can be trusted.

    id: str
    label: str
    unit: str
    current: float | None
    baseline: float | None
    change: float | None = None
    status: str | None = None  # for signals: detected, not_detected or insufficient_data
    q_value: float | None = None
    sample_size: tuple[int, int] | None = None  # (current, baseline)
