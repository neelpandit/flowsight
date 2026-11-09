"""Runs every scenario against a real model and grades the answers.

    python -m evals.run [--trials 2] [--model gemini-3.5-flash] [--scenario stable]

Each scenario is a synthetic repository with one planted cause. Each trial goes through the
production path (analytics → evidence → LLM → validation → repair → confidence cap) with an empty
cache, and is graded on one thing: did the model name the planted cause? A markdown report and a
JSON summary go to evals/reports/.
"""

# Decision: evals call a real model, not a fake, because the question is how the model behaves;
# the unit tests already cover the code with fakes. The grader is deterministic (the cause is
# one of a fixed list) rather than an LLM judge, so a score means the same thing every run.
# Several trials per scenario, because one answer from a non-deterministic model proves little.
import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr

from evals.scenarios import PERIOD, SCENARIOS, Scenario
from flowsight.analytics.report import build_flow_report
from flowsight.config import Settings, load_settings
from flowsight.insights.llm import OpenAICompatibleModel
from flowsight.insights.prompt import PROMPT_VERSION
from flowsight.insights.service import Explanation, InsightService
from flowsight.store import PullRequestStore, open_database

REPORTS = Path(__file__).parent / "reports"
SUMMARY = REPORTS / "latest.json"
RATE_LIMIT_PAUSES_S = (30, 60, 120)


@dataclass(frozen=True)
class Grade:
    scenario: str
    trial: int
    cause: str  # what the model said, or "(no answer)" when every model was unavailable
    answered: bool
    cause_correct: bool
    seconds: float


def grade(scenario: Scenario, trial: int, explanation: Explanation, seconds: float) -> Grade:
    answered = explanation.generator == "llm"
    cause = explanation.insight.hypothesis.cause if answered else "(no answer)"
    correct = answered and cause in scenario.expected_causes
    return Grade(scenario.name, trial, cause, answered, correct, seconds)


def models_from(settings: Settings, override: str | None) -> list[OpenAICompatibleModel]:
    """The main model and its backups, as the app uses them; `--model` replaces the main one."""
    names = dict.fromkeys([override or settings.llm_model, *settings.llm_backup_models])
    return [
        OpenAICompatibleModel(
            api_key=(settings.llm_api_key or SecretStr("")).get_secret_value(),
            base_url=settings.llm_base_url,
            model=name,
        )
        for name in names
    ]


async def run_trial(models: list[OpenAICompatibleModel], scenario: Scenario, trial: int) -> Grade:
    """One graded answer. Free-tier quota errors say nothing about the model, so a trial that
    hit them is retried after a pause rather than graded."""
    report = build_flow_report(scenario.pull_requests(), PERIOD)
    for wait_s in (*RATE_LIMIT_PAUSES_S, None):
        store = PullRequestStore(open_database(":memory:"))  # no cache
        service = InsightService(models[0], store, backups=models[1:])
        started = time.perf_counter()
        explanation = await service.explain("example/repo", report, report.flows)
        rate_limited = any("RateLimitError" in reason for reason in explanation.rejected_because)
        if not rate_limited or wait_s is None:
            return grade(scenario, trial, explanation, time.perf_counter() - started)
        print(f"  rate limited; pausing {wait_s}s before retrying", flush=True)
        await asyncio.sleep(wait_s)
    raise AssertionError("unreachable")


def rate(values: list[bool]) -> str:
    return f"{sum(values)}/{len(values)}"


def answered(grades: list[Grade]) -> list[Grade]:
    return [g for g in grades if g.answered]


def render(grades: list[Grade], model: str, trials: int) -> str:
    lines = [
        f"# Eval report: {model}, prompt {PROMPT_VERSION}",
        "",
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC · {trials} trial(s) per scenario · "
        f"{len(grades) - len(answered(grades))} run(s) got no answer (every model unavailable) "
        "and are left out of the scores",
        "",
        "| Scenario | Planted cause | Cause the AI gave | Right |",
        "|---|---|---|---|",
    ]
    for scenario in SCENARIOS:
        rows = [g for g in grades if g.scenario == scenario.name]
        if rows:
            lines.append(
                f"| {scenario.name} | {', '.join(sorted(scenario.expected_causes))} "
                f"| {', '.join(sorted({g.cause for g in rows}))} "
                f"| {rate([g.cause_correct for g in answered(rows)])} |"
            )
    lines += ["", "Scenarios:", ""]
    lines += [
        f"- **{s.name}:** {s.story}" for s in SCENARIOS if any(g.scenario == s.name for g in grades)
    ]
    lines += ["", f"**Overall:** cause right {rate([g.cause_correct for g in answered(grades)])}."]
    return "\n".join(lines) + "\n"


def save_summary(key: str, summary: dict[str, object]) -> None:
    """Writes the suite's result as JSON beside the markdown report."""
    existing = json.loads(SUMMARY.read_text()) if SUMMARY.exists() else {}
    existing[key] = summary
    SUMMARY.write_text(json.dumps(existing, indent=1) + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    parser.add_argument("--trials", type=int, default=2)
    parser.add_argument("--model", help="override LLM_MODEL")
    parser.add_argument("--scenario", action="append", help="run only these (repeatable)")
    args = parser.parse_args()

    settings = load_settings(github_token="unused")  # noqa: S106 - evals never call GitHub
    if settings.llm_api_key is None:
        raise SystemExit("Set LLM_API_KEY (see .env.example) to run the evals.")
    models = models_from(settings, args.model)
    model = models[0]
    chosen = [s for s in SCENARIOS if not args.scenario or s.name in args.scenario]
    grades = []
    try:
        for scenario in chosen:
            for trial in range(1, args.trials + 1):
                result = await run_trial(models, scenario, trial)
                grades.append(result)
                print(
                    f"{scenario.name:17} #{trial}  cause={result.cause:22} "
                    f"right={result.cause_correct!s:5} ({result.seconds:.0f}s)",
                    flush=True,
                )
    finally:
        for candidate in models:
            await candidate.aclose()

    report = render(grades, model.name, args.trials)
    REPORTS.mkdir(exist_ok=True)
    path = REPORTS / f"{model.name}.{PROMPT_VERSION}.md"
    path.write_text(report)
    if not args.scenario:
        save_summary("insights", {
            "model": model.name, "prompt_version": PROMPT_VERSION,
            "date": datetime.now(UTC).date().isoformat(), "trials": args.trials,
            "right": sum(g.cause_correct for g in grades), "total": len(answered(grades)),
            "no_answer": len(grades) - len(answered(grades)),
            "scenarios": [
                {"name": s.name, "story": s.story, "planted": sorted(s.expected_causes),
                 "right": sum(g.cause_correct for g in grades if g.scenario == s.name),
                 "total": sum(1 for g in answered(grades) if g.scenario == s.name)}
                for s in SCENARIOS
            ],
        })  # fmt: skip
    print(f"\n{report}\nWritten to {path}")


if __name__ == "__main__":
    asyncio.run(main())
