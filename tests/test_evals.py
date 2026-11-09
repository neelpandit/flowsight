"""Keeps the eval scenarios honest: each must plant exactly the signals of its story."""

import pytest

from evals.run import grade
from evals.scenarios import PERIOD, SCENARIOS, Scenario
from flowsight.analytics.report import build_flow_report
from flowsight.insights.confidence import Confidence
from flowsight.insights.schema import Hypothesis, LlmInsight
from flowsight.insights.service import Explanation

EXPECTED_SIGNALS = {
    "reviewer_absence": {"wait_for_first_review_hours"},
    "larger_changes": {"wait_for_first_review_hours"},
    "demand_spike": {"wait_for_first_review_hours"},
    "stable": set(),
    "tiny_sample": set(),
}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_each_scenario_plants_only_its_own_signals(scenario: Scenario) -> None:
    report = build_flow_report(scenario.pull_requests(), PERIOD)
    detected = {signal.metric for signal in report.signals if signal.status == "detected"}
    assert detected == EXPECTED_SIGNALS[scenario.name]


def explanation(cause: str, generator: str = "llm") -> Explanation:
    insight = LlmInsight(
        headline="h", summary="s", changes=[], findings=[], caveats=[],
        hypothesis=Hypothesis(statement="h", cause=cause, reasoning="r", confidence=0.8,
                              evidence=[]),
    )  # fmt: skip
    confidence = Confidence(0.8, 0.8, 0.85, "test", "high")
    return Explanation(insight, [], confidence, generator, "m", "v", [], False, 1)  # type: ignore[arg-type]


def test_the_grade_is_whether_the_planted_cause_was_named() -> None:
    scenario = next(s for s in SCENARIOS if s.name == "reviewer_absence")
    assert grade(scenario, 1, explanation("reviewer_capacity"), 1.0).cause_correct
    assert not grade(scenario, 1, explanation("demand_spike"), 1.0).cause_correct
    fallback = grade(scenario, 1, explanation("reviewer_capacity", "fallback"), 1.0)
    assert (fallback.cause, fallback.cause_correct) == ("(no answer)", False)
