"""Bounds the model's self-reported confidence by what the statistics can actually support.

LLM self-confidence is poorly calibrated, so the confidence we report is the lower of the
model's number and a ceiling derived from the cited evidence. Root causes are inferred from
correlations, so nothing goes above MAX_CONFIDENCE.

Both numbers are coarse (models say 0.75 or 0.85; the ceiling is one of three values), so
people see a level with its reason; the numbers stay in the API for evals and calibration.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from flowsight.insights.schema import EvidenceItem, Hypothesis

# Decision: never above 0.85. The statistics prove a number changed; the *cause* is the model's
# inference from correlations, and the data never contains a controlled experiment. A
# reported 0.95 would claim more than the data can show.
MAX_CONFIDENCE = 0.85
# Decision: a level, not a number, is shown. Both inputs are coarse (models say 0.75 or 0.85; the
# ceiling is one of a few steps), so "0.70" would suggest precision that isn't there.
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


# Decision: three rules, so the cap can be explained in one breath. The hypothesis either agrees
# with the statistics (it explains a real change, or it says nothing changed and nothing did),
# rests on nothing real (it cites a change that was not flagged, or no metric could be tested),
# or contradicts them (it says nothing changed while a real change was flagged).
AGREES, UNSUPPORTED, CONTRADICTS = MAX_CONFIDENCE, 0.4, 0.2


def _ceiling(hypothesis: Hypothesis, evidence: Sequence[EvidenceItem]) -> tuple[float, str]:
    signals = [item for item in evidence if item.status is not None]
    detected = {item.id for item in signals if item.status == "detected"}
    tested = any(item.q_value is not None for item in signals)
    if hypothesis.cause == "no_significant_change":
        if detected:
            return CONTRADICTS, "it says nothing changed, but the statistics show a real change"
        if not tested:
            return UNSUPPORTED, "there is too little data to tell whether anything changed"
        return AGREES, "no wait time changed in a statistically real way"
    cited = detected & set(hypothesis.evidence)
    if not cited:
        return UNSUPPORTED, "the change it explains is not statistically real"
    strongest = min(
        item.q_value for item in signals if item.id in cited and item.q_value is not None
    )
    return AGREES, (
        f"the change it explains is statistically real (q={strongest:g}), but the cause is "
        "inferred from correlations"
    )
