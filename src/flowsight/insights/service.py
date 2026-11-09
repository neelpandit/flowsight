import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from flowsight.analytics.metrics import PullRequestFlow
from flowsight.analytics.report import FlowReport
from flowsight.insights.confidence import Confidence, bounded_confidence
from flowsight.insights.evidence import build_evidence
from flowsight.insights.llm import InsightModel, LlmError
from flowsight.insights.prompt import PROMPT_VERSION, SYSTEM_PROMPT, user_message
from flowsight.insights.schema import EvidenceItem, Finding, Hypothesis, LlmInsight
from flowsight.insights.validation import validate
from flowsight.store import PullRequestStore
from flowsight.triage.schema import TriagedPullRequest
from flowsight.workmix.schema import WorkMix

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Explanation:
    insight: LlmInsight
    evidence: list[EvidenceItem]
    confidence: Confidence
    generator: Literal["llm", "fallback"]
    model: str
    prompt_version: str
    # Checks that failed, if the LLM's answers were rejected and the fallback used instead.
    rejected_because: list[str]
    cached: bool
    attempts: int  # calls to the model that answered: 0 when cached, 2 when a repair was needed


class InsightService:
    """Explains a flow report: evidence → LLM → validation → (one repair) → bounded confidence.

    An answer that fails validation twice is never shown. Instead the caller gets a
    deterministic summary of the detected signals, marked `generator="fallback"`. Validated
    answers are cached by prompt version, model and evidence, so a repeat costs nothing.

    Backup models are tried, in order, only when a model is unavailable (rate limited, out of
    quota, unreachable): quotas are per model, so a backup often still answers.
    """

    def __init__(
        self,
        model: InsightModel,
        store: PullRequestStore,
        *,
        backups: Sequence[InsightModel] = (),
    ) -> None:
        self._models = [model, *backups]
        self._store = store

    async def explain(
        self,
        repository: str,
        report: FlowReport,
        flows: Sequence[PullRequestFlow],
        triaged: Sequence[TriagedPullRequest] = (),
        work_mix: WorkMix | None = None,
    ) -> Explanation:
        evidence = build_evidence(report, flows, triaged, work_mix)
        prompt = user_message(repository, report.period, evidence)

        for model in self._models:  # a validated answer from any configured model will do
            cached = self._store.cached_insight(_cache_key(model, prompt))
            if cached is not None:
                insight = LlmInsight.model_validate_json(cached)
                return self._explanation(model, insight, evidence, attempts=0)

        unavailable: list[str] = []
        for model in self._models:
            try:
                insight, issues, attempts = await self._generate(model, prompt, evidence)
            except LlmError as error:
                logger.warning("%s unavailable: %s", model.name, error)
                unavailable.append(f"{model.name} unavailable: {error}")
                continue
            if issues:
                logger.warning("insight failed validation twice, using the fallback: %s", issues)
                return self._fallback(model, evidence, issues, attempts)
            self._store.save_insight(
                _cache_key(model, prompt), model.name, PROMPT_VERSION, insight.model_dump_json()
            )
            return self._explanation(model, insight, evidence, attempts=attempts)

        logger.warning("no model available, using the fallback summary")
        return self._fallback(self._models[0], evidence, unavailable, attempts=0)

    async def _generate(
        self, model: InsightModel, prompt: str, evidence: list[EvidenceItem]
    ) -> tuple[LlmInsight, list[str], int]:
        """An answer, the checks it still fails, and how many calls it took."""
        insight = await model.generate(SYSTEM_PROMPT, prompt, LlmInsight)
        issues = validate(insight, evidence)
        if not issues:
            return insight, [], 1
        logger.info("insight failed validation, asking for a repair: %s", issues)
        insight = await model.generate(SYSTEM_PROMPT, _repair_request(prompt, issues), LlmInsight)
        return insight, validate(insight, evidence), 2

    def _explanation(
        self,
        model: InsightModel,
        insight: LlmInsight,
        evidence: list[EvidenceItem],
        *,
        attempts: int,
    ) -> Explanation:
        confidence = bounded_confidence(insight.hypothesis, evidence)
        return Explanation(
            insight,
            evidence,
            confidence,
            "llm",
            model.name,
            PROMPT_VERSION,
            [],
            attempts == 0,
            attempts,
        )

    def _fallback(
        self, model: InsightModel, evidence: list[EvidenceItem], issues: list[str], attempts: int
    ) -> Explanation:
        insight = fallback_insight(evidence)
        confidence = Confidence(0.0, 0.0, 0.0, "no hypothesis was generated", "low")
        return Explanation(
            insight, evidence, confidence, "fallback", model.name, PROMPT_VERSION,
            issues, False, attempts,
        )  # fmt: skip


def _cache_key(model: InsightModel, prompt: str) -> str:
    """Any change to the instructions, the model or the evidence asks again."""
    parts = [PROMPT_VERSION, SYSTEM_PROMPT, model.name, prompt]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _repair_request(prompt: str, issues: Sequence[str]) -> str:
    problems = "\n".join(f"- {issue}" for issue in issues)
    return (
        f"{prompt}\n\nA previous answer failed these checks. Answer again and fix them:\n{problems}"
    )


def fallback_insight(evidence: Sequence[EvidenceItem]) -> LlmInsight:
    """A plain listing of the detected signals, and of who has the next move on the open pull
    requests read, used whenever no validated LLM answer exists."""
    detected = [item for item in evidence if item.status == "detected"]
    next_moves = [item for item in evidence if "where the next move is with" in item.label]
    return LlmInsight(
        headline=(
            f"{len(detected)} significant change(s) detected"
            if detected
            else "No significant change detected"
        ),
        summary=(
            "The AI explanation is unavailable, so this lists the detected signals "
            "without a root-cause hypothesis."
        ),
        changes=[],
        findings=[
            Finding(claim=f"{item.label}: {item.baseline} → {item.current}", evidence=[item.id])
            for item in detected
        ]
        + [
            Finding(claim=f"{item.label}: {item.current}", evidence=[item.id])
            for item in next_moves
        ],
        hypothesis=Hypothesis(
            statement="No hypothesis: the AI step was unavailable or its answer failed validation.",
            cause="other" if detected else "no_significant_change",
            reasoning="",
            confidence=0.0,
            evidence=[],
            alternatives_considered=[],
            how_to_verify="",
        ),
        recommended_actions=[],
        caveats=["Generated without the language model."],
    )
