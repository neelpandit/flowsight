"""Response bodies. Dataclasses double as the OpenAPI schema served at /docs."""

# Decision: response bodies are declared once as typed dataclasses, and FastAPI turns them into
# the OpenAPI schema at /docs. The documentation then can't drift from what is actually returned.

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from flowsight.analytics.metrics import Period, WindowMetrics
from flowsight.analytics.signals import Signal
from flowsight.insights.confidence import Confidence
from flowsight.insights.schema import Cause, ChangeExplained, EvidenceItem, Finding
from flowsight.triage.schema import TriagedPullRequest


@dataclass(frozen=True, slots=True)
class FlowResponse:
    repository: str
    data_as_of: datetime  # when the stored data was last synced from GitHub
    refreshing: bool  # a background refresh is under way; repeat the call for fresher data
    period: Period
    baseline_period: Period
    current: WindowMetrics
    baseline: WindowMetrics
    signals: list[Signal]


@dataclass(frozen=True, slots=True)
class JobView:
    status: Literal["running", "succeeded", "failed"]
    started_at: datetime
    finished_at: datetime | None
    error: str | None


@dataclass(frozen=True, slots=True)
class SyncStatus:
    repository: str
    covered_from: datetime | None
    synced_at: datetime | None
    job: JobView | None


@dataclass(frozen=True, slots=True)
class Accepted:
    status: Literal["syncing"]
    repository: str
    started_at: datetime
    retry_after_seconds: int
    status_url: str


@dataclass(frozen=True, slots=True)
class GeneratedBy:
    kind: Literal["llm", "fallback"]  # fallback: no validated LLM answer, signals listed plainly
    model: str
    prompt_version: str
    cached: bool


@dataclass(frozen=True, slots=True)
class HypothesisView:
    statement: str
    cause: Cause
    reasoning: str
    confidence: Confidence
    evidence: list[str]


@dataclass(frozen=True, slots=True)
class InsightResponse:
    repository: str
    data_as_of: datetime
    period: Period
    baseline_period: Period
    generated_by: GeneratedBy
    headline: str
    summary: str
    changes: list[ChangeExplained]  # one per detected signal: why it changed
    findings: list[Finding]
    hypothesis: HypothesisView
    caveats: list[str]
    # Every fact the model was given; each ID cited above resolves to one of these.
    evidence: list[EvidenceItem]
    # Why the LLM's answers were rejected, when generated_by.kind is "fallback".
    rejected_because: list[str]


@dataclass(frozen=True, slots=True)
class TriageResponse:
    repository: str
    data_as_of: datetime
    period: Period
    models: list[str]  # the models whose readings are shown
    prompt_version: str
    cached: int  # readings reused from the cache rather than asked again
    # The still-open pull requests that waited longest, longest first.
    pull_requests: list[TriagedPullRequest]
    # Pull requests read but left out, and why (failed checks, model unavailable).
    not_shown: list[str]
