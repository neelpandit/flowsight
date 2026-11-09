"""Deterministic checks that an LLM answer is grounded in the evidence it was given."""

import re
from collections.abc import Iterable, Sequence

from flowsight.insights.schema import EvidenceItem, LlmInsight
from flowsight.triage.validation import MENTION

_EVIDENCE_ID = re.compile(r"\b[SCUT]\d+\b")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")


def validate(insight: LlmInsight, evidence: Sequence[EvidenceItem]) -> list[str]:
    """Problems that make an answer unfit to show: citations of facts that don't exist, claims
    that cite nothing, numbers that appear nowhere in the evidence, people who appear nowhere in
    it, and an impossible confidence. An empty list means the answer passed."""
    known = {item.id for item in evidence}
    issues: list[str] = []

    issues += _change_issues(insight, evidence)
    cited: list[tuple[str, list[str]]] = [
        *((f"change {c.signal}", c.evidence) for c in insight.changes),
        *((f"finding {i}", f.evidence) for i, f in enumerate(insight.findings, start=1)),
        ("hypothesis", insight.hypothesis.evidence),
        *((f"action {i}", a.evidence) for i, a in enumerate(insight.recommended_actions, start=1)),
    ]
    for where, ids in cited:
        if not ids:
            issues.append(f"{where} cites no evidence")
        unknown = sorted(set(ids) - known)
        if unknown:
            issues.append(f"{where} cites unknown evidence {', '.join(unknown)}")

    if any(item.id.startswith("T") for item in evidence):  # conversations were read
        if not any(evidence_id.startswith("T") for evidence_id in insight.hypothesis.evidence):
            issues.append("hypothesis cites no conversation (T) evidence although some was given")
        issues += [
            f"finding {i} only restates statistics (S); connect it to C- or T-items"
            for i, finding in enumerate(insight.findings, start=1)
            if finding.evidence and all(e.startswith("S") for e in finding.evidence)
        ]

    issues += _people_issues(insight, evidence)
    allowed = _allowed_numbers(evidence)
    for text in _texts(insight):
        for number in _numbers(text):
            if not any(_close(number, value) for value in allowed):
                issues.append(f"number {number:g} does not appear in the evidence")

    if not 0 <= insight.hypothesis.confidence <= 1:
        issues.append("confidence must be between 0 and 1")
    return sorted(set(issues))


def _texts(insight: LlmInsight) -> Iterable[str]:
    yield insight.headline
    yield insight.summary
    yield from (change.explanation for change in insight.changes)
    yield from (finding.claim for finding in insight.findings)
    yield insight.hypothesis.statement
    yield insight.hypothesis.reasoning
    yield from insight.hypothesis.alternatives_considered
    yield insight.hypothesis.how_to_verify
    yield from (action.action for action in insight.recommended_actions)
    yield from insight.caveats


def _change_issues(insight: LlmInsight, evidence: Sequence[EvidenceItem]) -> list[str]:
    """Each change the statistics call real gets exactly one explanation, and nothing else does."""
    detected = {item.id for item in evidence if item.status == "detected"}
    given = [change.signal for change in insight.changes]
    issues = [f"no explanation for the detected signal {s}" for s in sorted(detected - set(given))]
    issues += [f"{s} is not a detected signal, so it needs no explanation"
               for s in sorted(set(given) - detected)]  # fmt: skip
    issues += [
        f"{s} is explained more than once" for s in sorted({s for s in given if given.count(s) > 1})
    ]
    return issues


def _people_issues(insight: LlmInsight, evidence: Sequence[EvidenceItem]) -> list[str]:
    """Owners and @-mentions must be people the evidence names."""
    known = " ".join(part.lower() for item in evidence for part in (item.label, item.detail or ""))
    issues = [
        f"action {i} owner {action.owner} does not appear in the evidence"
        for i, action in enumerate(insight.recommended_actions, start=1)
        if action.owner and action.owner.lstrip("@").lower() not in known
    ]
    issues += [
        f"@{login} does not appear in the evidence"
        for text in _texts(insight)
        for login in MENTION.findall(text)
        if login.lower() not in known
    ]
    return issues


def _numbers(text: str) -> list[float]:
    text = _EVIDENCE_ID.sub(" ", text)
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)  # 1,509 -> 1509
    return [float(match) for match in _NUMBER.findall(text)]


def _allowed_numbers(evidence: Sequence[EvidenceItem]) -> set[float]:
    """Every number in the evidence, plus the ways a writer naturally restates them:
    percentages, differences, multiples and rounded values."""
    allowed: set[float] = {0.0, 1.0}  # "no reviews", "one reviewer"
    for item in evidence:
        for text in (item.detail, item.untrusted_title):  # versions, issue numbers, durations
            if text:
                allowed.update(_numbers(text))
        values = [item.current, item.baseline, item.change, item.q_value,
                  item.pull_request, item.lines_changed, *(item.sample_size or ())]  # fmt: skip
        allowed.update(float(v) for v in values if v is not None)
        if item.change is not None:
            allowed.update({abs(item.change) * 100, abs(item.change) * 100 + 100})
        if item.unit == "ratio":
            allowed.update(
                float(v) * 100 for v in (item.current, item.baseline, item.change) if v is not None
            )
        if item.current is not None and item.baseline is not None:
            allowed.add(abs(item.current - item.baseline))
            if item.baseline:
                allowed.add(item.current / item.baseline)
            if item.current:
                allowed.add(item.baseline / item.current)
    allowed.update({round(value) for value in allowed} | {round(value, 1) for value in allowed})
    return allowed


def _close(number: float, value: float) -> bool:
    return abs(number - value) <= max(0.15, 0.03 * abs(value))
