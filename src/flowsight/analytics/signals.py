from collections.abc import Sequence
from dataclasses import dataclass, replace
from math import isnan
from statistics import median
from typing import Literal

from scipy.stats import binomtest, false_discovery_control, fisher_exact, mannwhitneyu

from flowsight.analytics.metrics import WindowMeasurement

# When a change counts as a signal rather than noise: enough data on both sides, a change big
# enough to matter, and statistical support that survives correction for testing several
# metrics at once.
MIN_SAMPLES = 10
MIN_RELATIVE_CHANGE = 0.25
MIN_HOURS_CHANGE = 1.0
MIN_RATE_CHANGE = 0.10
FALSE_DISCOVERY_RATE = 0.05
CONCENTRATION_SHARE = 0.5
MIN_REVIEWS_FOR_CONCENTRATION = 20

Category = Literal["velocity", "flow", "capacity", "governance", "dependency_risk"]
Status = Literal["detected", "not_detected", "insufficient_data"]


@dataclass(frozen=True, slots=True)
class SampleSize:
    current: int
    baseline: int


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    category: Category
    metric: str
    description: str
    unit: Literal["hours", "pull_requests", "ratio"]
    current: float | None
    baseline: float | None
    change: float | None  # relative for hours and counts (0.5 = +50%); absolute for ratios
    test: Literal["rank_sum", "fisher_exact", "binomial", "threshold"]
    p_value: float | None
    q_value: float | None  # p-value adjusted for the number of metrics tested (Benjamini-Hochberg)
    sample_size: SampleSize
    status: Status


@dataclass(frozen=True, slots=True)
class _Draft:
    signal: Signal
    material: bool  # whether the change is big enough to matter, regardless of significance


def _significant(value: float) -> float:
    """Three significant figures, so very small p-values stay visible instead of rounding to 0."""
    return float(f"{value:.3g}")


def _relative(current: float, baseline: float) -> float | None:
    return round(current / baseline - 1, 3) if baseline > 0 else None


def _duration_shift(
    category: Category,
    metric: str,
    description: str,
    current: Sequence[float],
    baseline: Sequence[float],
) -> _Draft:
    now = round(median(current), 1) if current else None
    before = round(median(baseline), 1) if baseline else None
    change = _relative(now, before) if now is not None and before is not None else None
    enough = len(current) >= MIN_SAMPLES and len(baseline) >= MIN_SAMPLES
    # Rank-sum rather than a median test: it uses every observation, so it keeps its power on
    # review times that mix pickups within minutes and waits of days.
    p_value = _rank_sum_p_value(current, baseline) if enough else None
    material = (
        now is not None
        and before is not None
        and change is not None
        and abs(change) >= MIN_RELATIVE_CHANGE
        and abs(now - before) >= MIN_HOURS_CHANGE
    )
    signal = Signal(
        "", category, metric, description, "hours", now, before, change, "rank_sum",
        p_value, None, SampleSize(len(current), len(baseline)), "insufficient_data",
    )  # fmt: skip
    return _Draft(signal, material)


def _rank_sum_p_value(current: Sequence[float], baseline: Sequence[float]) -> float:
    p_value = float(mannwhitneyu(current, baseline).pvalue)
    # All values tied (e.g. every approval at first review): nothing varies, so no evidence.
    return 1.0 if isnan(p_value) else p_value


def _rate_shift(
    category: Category,
    metric: str,
    description: str,
    current: Sequence[bool],
    baseline: Sequence[bool],
) -> _Draft:
    now = round(sum(current) / len(current), 3) if current else None
    before = round(sum(baseline) / len(baseline), 3) if baseline else None
    change = round(now - before, 3) if now is not None and before is not None else None
    enough = len(current) >= MIN_SAMPLES and len(baseline) >= MIN_SAMPLES
    table = [
        [sum(current), len(current) - sum(current)],
        [sum(baseline), len(baseline) - sum(baseline)],
    ]
    signal = Signal(
        "", category, metric, description, "ratio", now, before, change, "fisher_exact",
        float(fisher_exact(table).pvalue) if enough else None, None,
        SampleSize(len(current), len(baseline)), "insufficient_data",
    )  # fmt: skip
    return _Draft(signal, change is not None and abs(change) >= MIN_RATE_CHANGE)


def _count_shift(
    category: Category, metric: str, description: str, current: int, baseline: int
) -> _Draft:
    """Event counts in two equally long windows: under "no change" the split is Binomial(n, 0.5)."""
    change = _relative(current, baseline)
    enough = current + baseline >= 2 * MIN_SAMPLES
    signal = Signal(
        "", category, metric, description, "pull_requests", current, baseline, change, "binomial",
        float(binomtest(current, current + baseline).pvalue) if enough else None, None,
        SampleSize(current, baseline), "insufficient_data",
    )  # fmt: skip
    return _Draft(signal, change is not None and abs(change) >= MIN_RELATIVE_CHANGE)


def detect_signals(current: WindowMeasurement, baseline: WindowMeasurement) -> list[Signal]:
    """Detects meaningful changes between the current window and its baseline, plus one standing
    risk: review work concentrated on a single person."""
    now, before = current.samples, baseline.samples
    drafts = [
        _count_shift("velocity", "merged_pull_requests", "Human-authored pull requests merged",
                     current.metrics.merged, baseline.metrics.merged),
        _duration_shift("flow", "wait_for_first_review_hours",
                        "Median hours from ready-for-review to first review",
                        now.wait_for_first_review, before.wait_for_first_review),
        _duration_shift("flow", "review_to_approval_hours",
                        "Median hours from first review to first approval",
                        now.review_to_approval, before.review_to_approval),
        _duration_shift("flow", "approval_to_merge_hours",
                        "Median hours from last approval to merge",
                        now.approval_to_merge, before.approval_to_merge),
        _duration_shift("capacity", "external_wait_for_first_review_hours",
                        "Median wait for first review on external contributors' pull requests",
                        now.wait_for_first_review_external, before.wait_for_first_review_external),
        _duration_shift("capacity", "maintainer_wait_for_first_review_hours",
                        "Median wait for first review on maintainers' pull requests",
                        now.wait_for_first_review_maintainers,
                        before.wait_for_first_review_maintainers),
        _rate_shift("governance", "merged_without_review_rate",
                    "Share of merged pull requests with no review from anyone but the author",
                    [not r for r in now.reviewed], [not r for r in before.reviewed]),
    ]  # fmt: skip

    tested = [i for i, draft in enumerate(drafts) if draft.signal.p_value is not None]
    p_values = [drafts[i].signal.p_value or 1.0 for i in tested]
    q_values: list[float] = list(false_discovery_control(p_values)) if tested else []
    q_by_index = dict(zip(tested, q_values, strict=True))

    signals = []
    for index, draft in enumerate(drafts):
        q = q_by_index.get(index)
        status: Status = (
            "insufficient_data"
            if q is None
            else "detected"
            if draft.material and q < FALSE_DISCOVERY_RATE
            else "not_detected"
        )
        p = draft.signal.p_value
        signals.append(
            replace(
                draft.signal,
                id=f"S{index + 1}",
                p_value=None if p is None else _significant(p),
                q_value=None if q is None else _significant(q),
                status=status,
            )
        )
    signals.append(_concentration(f"S{len(signals) + 1}", current, baseline))
    return signals


def _concentration(
    signal_id: str, current: WindowMeasurement, baseline: WindowMeasurement
) -> Signal:
    now, before = current.metrics.reviewers, baseline.metrics.reviewers
    change = (
        round(now.top_share - before.top_share, 3)
        if now.top_share is not None and before.top_share is not None
        else None
    )
    status: Status = (
        "insufficient_data"
        if now.reviews < MIN_REVIEWS_FOR_CONCENTRATION
        else "detected"
        if (now.top_share or 0) >= CONCENTRATION_SHARE
        else "not_detected"
    )
    return Signal(
        signal_id, "dependency_risk", "top_reviewer_share",
        "Share of all reviews done by the single busiest reviewer", "ratio",
        now.top_share, before.top_share, change, "threshold", None, None,
        SampleSize(now.reviews, before.reviews), status,
    )  # fmt: skip
