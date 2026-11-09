from collections.abc import Sequence
from dataclasses import dataclass, replace
from math import isnan
from statistics import median
from typing import Literal

from scipy.stats import false_discovery_control, mannwhitneyu

from flowsight.analytics.metrics import WindowMeasurement

# Decision: only wait times are tested, because the question is "where does review work wait?".
# Other numbers (merges, merges without review, who reviews) are shown and given to the model as
# context, but they are never flagged as changes: they are possible causes of waiting, not waiting.
#
# Decision: a change becomes a signal only when all three criteria hold.
#   1. Enough data on both sides (MIN_SAMPLES). Below about 10 pull requests a single slow
#      review moves the median by days, and the test has too little power to say anything.
#   2. Big enough to matter (MIN_RELATIVE_CHANGE and MIN_HOURS_CHANGE). With hundreds of pull
#      requests a tiny shift becomes statistically "real" but is not worth anyone's attention.
#      25% is roughly the smallest change a team lead would act on, and the 1-hour floor stops
#      0.4h -> 0.6h (+50%) from counting.
#   3. Unlikely to be luck after correcting for testing three metrics at once (q < 0.05). At a
#      plain 5% bar per metric, the chance that at least one of three looks real by luck is
#      about 14% (1 - 0.95^3), and the LLM would explain each false alarm with confidence.
#      Benjamini-Hochberg is used instead of Bonferroni because it controls the share of false
#      discoveries without making the bar so strict that real changes are missed. With three
#      tests the two corrections are close; the choice matters more if metrics are added.
# Each criterion alone fails: (1) without (3) flags noise, (3) without (2) flags trivia on big
# repositories, and (2) without (3) flags any large swing on a quiet repository.
MIN_SAMPLES = 10
MIN_RELATIVE_CHANGE = 0.25
MIN_HOURS_CHANGE = 1.0
FALSE_DISCOVERY_RATE = 0.05

Category = Literal["flow"]
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
    unit: Literal["hours"]
    current: float | None  # median hours this period
    baseline: float | None  # median hours the period before
    change: float | None  # relative: 0.5 means +50%
    test: Literal["rank_sum"]
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
    # Decision: the median is what is reported, not the mean, because waits are skewed: one PR
    # left waiting for a month would turn a typical "4 hours" into a misleading "40 hours".
    now = round(median(current), 1) if current else None
    before = round(median(baseline), 1) if baseline else None
    change = _relative(now, before) if now is not None and before is not None else None
    enough = len(current) >= MIN_SAMPLES and len(baseline) >= MIN_SAMPLES
    # Decision: Mann-Whitney U (rank-sum) is the one test used. Review times are heavily skewed:
    # most pickups take minutes, a few take weeks. A t-test assumes roughly normal data and one
    # stuck PR would dominate it. Rank-sum compares the ranks of every observation, makes no
    # assumption about the shape of the distribution, and is not thrown by outliers.
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
    # All values tied (e.g. every pickup instant): nothing varies, so no evidence.
    return 1.0 if isnan(p_value) else p_value


def detect_signals(current: WindowMeasurement, baseline: WindowMeasurement) -> list[Signal]:
    """Detects meaningful changes in wait times between the current window and its baseline."""
    now, before = current.samples, baseline.samples
    # Decision: three metrics, one per stage where a merged pull request can wait: for its first
    # review, from that review to its first approval, and from its last approval to the merge.
    # The middle stage is often 0 (the first review is the approval) but is kept so the three
    # tiles cover the whole journey. A split by author (maintainers vs outside contributors) was
    # tried and dropped: GitHub's association is an open-source idea that says nothing inside a
    # company, where everyone is a member. The count of PRs still waiting at the end of the
    # period is not tested either: it is a snapshot on one day, not a sample across the period,
    # so it is shown as context instead.
    drafts = [
        _duration_shift("flow", "wait_for_first_review_hours",
                        "Median hours from ready-for-review to first review",
                        now.wait_for_first_review, before.wait_for_first_review),
        _duration_shift("flow", "review_to_approval_hours",
                        "Median hours from first review to first approval",
                        now.review_to_approval, before.review_to_approval),
        _duration_shift("flow", "approval_to_merge_hours",
                        "Median hours from last approval to merge",
                        now.approval_to_merge, before.approval_to_merge),
    ]  # fmt: skip

    # Decision: Benjamini-Hochberg over only the metrics that had enough data. Correcting over
    # untested metrics would make the bar stricter for no reason. The correction uses every
    # tested metric, including those too small to matter: dropping them first would mean
    # choosing which tests count after seeing the results.
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
    return signals
