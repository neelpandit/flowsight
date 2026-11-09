from collections.abc import Sequence
from dataclasses import dataclass

from flowsight.analytics.metrics import (
    Period,
    PullRequestFlow,
    WindowMetrics,
    measure_window,
)
from flowsight.analytics.signals import Signal, detect_signals
from flowsight.domain import PullRequest


@dataclass(frozen=True, slots=True)
class FlowReport:
    period: Period
    baseline_period: Period
    current: WindowMetrics
    baseline: WindowMetrics
    signals: list[Signal]
    flows: list[PullRequestFlow]  # the merged pull requests behind `current`


def build_flow_report(pull_requests: Sequence[PullRequest], period: Period) -> FlowReport:
    """Compares `period` with the equally long period before it.

    `pull_requests` must cover the baseline as well as the period itself.
    """
    baseline = period.previous()
    current = measure_window(pull_requests, period)
    previous = measure_window(pull_requests, baseline)
    return FlowReport(
        period=period,
        baseline_period=baseline,
        current=current.metrics,
        baseline=previous.metrics,
        signals=detect_signals(current, previous),
        flows=current.flows,
    )
