"""Bounds the model's self-reported confidence by what the statistics can actually support.

LLM self-confidence is poorly calibrated, so the confidence we report is the lower of the
model's number and a ceiling derived from the cited evidence. Root causes are inferred from
correlations, so nothing goes above MAX_CONFIDENCE.

Both numbers are coarse (models say 0.75 or 0.85; the ceiling is a step in a short table), so
people see a level with its reason; the numbers stay in the API for evals and calibration.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from flowsight.insights.schema import EvidenceItem, Hypothesis

MAX_CONFIDENCE = 0.85
HIGH, MODERATE = 0.75, 0.5

Level = Literal["low", "moderate", "high"]


@dataclass(frozen=True, slots=True)
class Confidence:
    value: float  # what we report: min(reported_by_model, evidence_ceiling)
    reported_by_model: float
    evidence_ceiling: float
    basis: str  # why the ceiling is where it is, in plain words
    level: Level  # what people see: the value's precision is not real


def level_of(value: float) -> Level:
    return "high" if value >= HIGH else "moderate" if value >= MODERATE else "low"


def bounded_confidence(hypothesis: Hypothesis, evidence: Sequence[EvidenceItem]) -> Confidence:
    ceiling, basis = _ceiling(hypothesis, evidence)
    reported = min(max(hypothesis.confidence, 0.0), 1.0)
    value = round(min(reported, ceiling), 2)
    return Confidence(value, reported, ceiling, basis, level_of(value))


def _ceiling(  # noqa: PLR0911 - a decision table reads best as early returns
    hypothesis: Hypothesis, evidence: Sequence[EvidenceItem]
) -> tuple[float, str]:
    signals = [item for item in evidence if item.status is not None]
    anything_detected = any(item.status == "detected" for item in signals)
    if hypothesis.cause == "no_significant_change":
        if anything_detected:
            return 0.2, "it says nothing changed, but the statistics show a real change"
        if not any(item.q_value is not None for item in signals):  # nothing could be tested
            return 0.3, "there is too little data to tell whether anything changed"
        return 0.8, "no metric changed in a statistically real way"

    cited = {item.id: item for item in signals if item.id in hypothesis.evidence}
    detected = [item for item in cited.values() if item.status == "detected"]
    if not detected:
        return 0.4, "the change it explains is not statistically clear"
    tested = [item for item in detected if item.q_value is not None]
    if not tested:
        return 0.75, "it rests on a standing risk, such as one person doing most reviews"
    strongest = min(item.q_value for item in tested if item.q_value is not None)
    smallest_sample = min(min(item.sample_size or (0, 0)) for item in tested)
    if strongest < 0.01 and smallest_sample >= 30:
        return MAX_CONFIDENCE, (
            f"the change it explains is clear in the statistics (q={strongest:g}, at least "
            f"{smallest_sample} pull requests a period); the cause is still inferred"
        )
    if strongest < 0.05:
        return 0.7, (
            f"the change it explains is statistically real (q={strongest:g}, at least "
            f"{smallest_sample} pull requests a period), but the cause is inferred"
        )
    return 0.5, "the change it explains is only borderline in the statistics"
