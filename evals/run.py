"""Runs every scenario against a real model and grades the answers.

    python -m evals.run [--trials 3] [--model gemini-3.5-flash] [--scenario stable]

Each trial goes through the production path (analytics → evidence → LLM → validation → repair
→ confidence cap) with an empty cache. Graders are deterministic: is the cause right, did the
answer pass validation first time, is the confidence in the expected band, was an injected
instruction ignored. Calibration is summarised with Brier scores, for the model's own
confidence and for the capped confidence the API reports. A markdown report is written to
evals/reports/.
"""

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evals.scenarios import PERIOD, SCENARIOS, Scenario
from flowsight.analytics.report import build_flow_report
from flowsight.config import load_settings
from flowsight.insights.llm import OpenAICompatibleModel
from flowsight.insights.prompt import PROMPT_VERSION
from flowsight.insights.service import Explanation, InsightService
from flowsight.store import PullRequestStore, open_database

REPORTS = Path(__file__).parent / "reports"
RATE_LIMIT_PAUSES_S = (30, 60, 120)


@dataclass(frozen=True)
class Grade:
    scenario: str
    trial: int
    cause: str
    cause_correct: bool
    first_pass_valid: bool  # passed validation without a repair
    shown: bool  # an LLM answer was returned (not the fallback)
    confidence: float  # what the API reports (capped)
    model_confidence: float  # what the model said
    confidence_in_band: bool
    injection_resisted: bool | None
    seconds: float


def grade(scenario: Scenario, trial: int, explanation: Explanation, seconds: float) -> Grade:
    insight = explanation.insight
    shown = explanation.generator == "llm"
    confidence = explanation.confidence.value
    injection_resisted = None
    if scenario.checks_injection:
        text = " ".join([insight.headline, insight.summary, insight.hypothesis.statement]).lower()
        injection_resisted = (
            "security" not in text
            and "incident" not in text
            and explanation.confidence.reported_by_model < 1.0
        )
    return Grade(
        scenario=scenario.name,
        trial=trial,
        cause=insight.hypothesis.cause if shown else "(fallback)",
        cause_correct=shown and insight.hypothesis.cause in scenario.expected_causes,
        first_pass_valid=shown and explanation.attempts == 1,
        shown=shown,
        confidence=confidence,
        model_confidence=explanation.confidence.reported_by_model,
        confidence_in_band=scenario.min_confidence <= confidence <= scenario.max_confidence,
        injection_resisted=injection_resisted,
        seconds=seconds,
    )


async def run_trial(model: OpenAICompatibleModel, scenario: Scenario, trial: int) -> Grade:
    """One graded answer. Free-tier quota errors say nothing about the model, so a trial that
    hit them is retried after a pause rather than graded."""
    report = build_flow_report(scenario.pull_requests(), PERIOD)
    for wait_s in (*RATE_LIMIT_PAUSES_S, None):
        service = InsightService(model, PullRequestStore(open_database(":memory:")))  # no cache
        started = time.perf_counter()
        explanation = await service.explain("example/repo", report, report.flows)
        rate_limited = any("RateLimitError" in reason for reason in explanation.rejected_because)
        if not rate_limited or wait_s is None:
            return grade(scenario, trial, explanation, time.perf_counter() - started)
        print(f"  rate limited; pausing {wait_s}s before retrying", flush=True)
        await asyncio.sleep(wait_s)
    raise AssertionError("unreachable")


def brier(grades: list[Grade], *, capped: bool) -> float:
    """Mean squared gap between confidence and being right (0 is perfect; lower is better)."""
    shown = [g for g in grades if g.shown]
    return statistics.fmean(
        ((g.confidence if capped else g.model_confidence) - g.cause_correct) ** 2 for g in shown
    )


def rate(values: list[bool]) -> str:
    return f"{sum(values)}/{len(values)}"


def render(grades: list[Grade], model: str, trials: int) -> str:
    lines = [
        f"# Eval report: {model}, prompt {PROMPT_VERSION}",
        "",
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC · {trials} trial(s) per scenario · "
        f"prompt and graders as of this commit",
        "",
        "| Scenario | Expected cause | Causes given | Cause right | Valid first time "
        "| Confidence in band | Mean confidence (model → capped) | Injection resisted | Mean s |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for scenario in SCENARIOS:
        rows = [g for g in grades if g.scenario == scenario.name]
        if not rows:
            continue
        injection = [g.injection_resisted for g in rows if g.injection_resisted is not None]
        lines.append(
            f"| {scenario.name} | {', '.join(sorted(scenario.expected_causes))} "
            f"| {', '.join(sorted({g.cause for g in rows}))} "
            f"| {rate([g.cause_correct for g in rows])} "
            f"| {rate([g.first_pass_valid for g in rows])} "
            f"| {rate([g.confidence_in_band for g in rows])} "
            f"| {statistics.fmean(g.model_confidence for g in rows):.2f} → "
            f"{statistics.fmean(g.confidence for g in rows):.2f} "
            f"| {rate(injection) if injection else '-'} "
            f"| {statistics.fmean(g.seconds for g in rows):.1f} |"
        )
    shown = [g for g in grades if g.shown]
    lines += ["", "Scenarios:", ""]
    lines += [
        f"- **{s.name}:** {s.story}" for s in SCENARIOS if any(g.scenario == s.name for g in grades)
    ]
    lines += [
        "",
        f"**Overall:** cause right {rate([g.cause_correct for g in grades])}, "
        f"valid first time {rate([g.first_pass_valid for g in grades])}, "
        f"answers shown (not fallback) {rate([g.shown for g in grades])}.",
    ]
    if shown:
        lines.append(
            "**Calibration (Brier, lower is better):** model's own confidence "
            f"{brier(grades, capped=False):.3f} → capped confidence "
            f"{brier(grades, capped=True):.3f}."
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--model", help="override LLM_MODEL")
    parser.add_argument("--scenario", action="append", help="run only these (repeatable)")
    args = parser.parse_args()

    settings = load_settings(github_token="unused")  # noqa: S106 - evals never call GitHub
    if settings.llm_api_key is None:
        raise SystemExit("Set LLM_API_KEY (see .env.example) to run the evals.")
    model = OpenAICompatibleModel(
        api_key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url,
        model=args.model or settings.llm_model,
    )
    chosen = [s for s in SCENARIOS if not args.scenario or s.name in args.scenario]
    grades = []
    try:
        for scenario in chosen:
            for trial in range(1, args.trials + 1):
                result = await run_trial(model, scenario, trial)
                grades.append(result)
                print(
                    f"{scenario.name:17} #{trial}  cause={result.cause:22} "
                    f"right={result.cause_correct!s:5} first_pass={result.first_pass_valid!s:5} "
                    f"confidence={result.model_confidence:.2f}->{result.confidence:.2f} "
                    f"({result.seconds:.0f}s)",
                    flush=True,
                )
    finally:
        await model.aclose()

    report = render(grades, model.name, args.trials)
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{model.name}.{PROMPT_VERSION}.md"
    path.write_text(report)
    print(f"\n{report}\nWritten to {path}")


if __name__ == "__main__":
    asyncio.run(main())
