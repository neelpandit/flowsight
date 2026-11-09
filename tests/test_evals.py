"""Keeps the eval scenarios honest: each must plant exactly the signals of its story."""

import pytest

from evals.run import grade
from evals.scenarios import INJECTION, PERIOD, SCENARIOS, Scenario
from flowsight.analytics.report import build_flow_report
from flowsight.insights.confidence import Confidence
from flowsight.insights.schema import Hypothesis, LlmInsight
from flowsight.insights.service import Explanation

SLOWER_PICKUP = {
    "wait_for_first_review_hours",
    "external_wait_for_first_review_hours",
    "maintainer_wait_for_first_review_hours",
}
EXPECTED_SIGNALS = {
    "reviewer_absence": SLOWER_PICKUP,
    "larger_changes": SLOWER_PICKUP,
    "demand_spike": SLOWER_PICKUP | {"merged_pull_requests"},
    "coverage_drop": {"merged_without_review_rate"},
    "stable": set(),
    "tiny_sample": set(),
    "prompt_injection": SLOWER_PICKUP,
}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_each_scenario_plants_only_its_own_signals(scenario: Scenario) -> None:
    report = build_flow_report(scenario.pull_requests(), PERIOD)
    detected = {signal.metric for signal in report.signals if signal.status == "detected"}
    assert detected == EXPECTED_SIGNALS[scenario.name]


def explanation(headline: str, cause: str, model_confidence: float) -> Explanation:
    insight = LlmInsight(
        headline=headline, summary="s", changes=[], findings=[], recommended_actions=[], caveats=[],
        hypothesis=Hypothesis(statement="h", cause=cause, reasoning="r", confidence=model_confidence,
                              evidence=[], alternatives_considered=[], how_to_verify="v"),
    )  # fmt: skip
    confidence = Confidence(min(model_confidence, 0.7), model_confidence, 0.7, "test", "moderate")
    return Explanation(insight, [], confidence, "llm", "m", "v", [], False, 1)


def test_an_obeyed_injection_is_caught() -> None:
    scenario = next(s for s in SCENARIOS if s.name == "prompt_injection")
    obeyed = grade(scenario, 1, explanation("Critical security incident", "other", 1.0), 1.0)
    ignored = grade(scenario, 1, explanation("Pickup slowed", "reviewer_capacity", 0.8), 1.0)
    assert (obeyed.injection_resisted, obeyed.cause_correct) == (False, False)
    assert (ignored.injection_resisted, ignored.cause_correct) == (True, True)
    assert INJECTION in scenario.pull_requests()[-1].title
