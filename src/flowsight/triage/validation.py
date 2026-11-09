"""Checks that each diagnosis is grounded in the conversation it describes."""

import re
from collections.abc import Iterable

from flowsight.triage.casefile import CaseFile
from flowsight.triage.schema import Diagnosis

MAX_QUOTE_WORDS = 40  # the prompt asks for 25; a little slack for long identifiers
MAX_REPLY_CHARS = 900
# Typographic quotes and apostrophes compare equal to plain ones.
_PLAIN_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"'})
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
# A GitHub @mention; not an npm scope such as @rollup/plugin-x, nor an email address.
MENTION = re.compile(r"(?<![\w.])@([A-Za-z0-9-]+)(?![\w/-])")


def validate_diagnosis(diagnosis: Diagnosis, case: CaseFile) -> list[str]:
    """Problems that make a diagnosis unfit to show; empty when it passes."""
    issues: list[str] = []
    if diagnosis.action != "wait" and not (diagnosis.reply or "").strip():
        issues.append("it needs a reply, unless it is waiting on something else")
    addressed = {login.lower() for login in MENTION.findall(diagnosis.reply or "")}
    actors = {login.lower() for login in (diagnosis.owner, case.conversation.author) if login}
    if diagnosis.reply and not addressed & actors:
        issues.append("the reply must @mention the owner or the author")
    if len(diagnosis.reply or "") > MAX_REPLY_CHARS:
        issues.append(f"the reply is longer than {MAX_REPLY_CHARS} characters")
    issues += _quote_issues(diagnosis, case)
    issues += _people_issues(diagnosis, case)
    allowed = _allowed_numbers(case)
    for text in _texts(diagnosis):
        for number in _numbers(text):
            if not any(abs(number - value) <= max(0.15, 0.03 * value) for value in allowed):
                issues.append(f"number {number:g} does not appear in the case file")
    return sorted({f"#{case.number}: {issue}" for issue in issues})


def _quote_issues(diagnosis: Diagnosis, case: CaseFile) -> Iterable[str]:
    """Every quote must be copied from the event it cites."""
    quotable = case.quotable()
    for quote in diagnosis.quotes:
        source = quotable.get(quote.event)
        if source is None:
            yield f"quote cites unknown event {quote.event}"
        elif _normal(quote.text) not in _normal(source):
            yield f"quote is not verbatim from {quote.event}: {quote.text!r}"
        elif len(quote.text.split()) > MAX_QUOTE_WORDS:
            yield f"quote from {quote.event} is too long"


def _people_issues(diagnosis: Diagnosis, case: CaseFile) -> Iterable[str]:
    """Only people the code vouches for may own a step; only people present may be named."""
    owners = {login.lower() for login in case.owners()}
    owner = diagnosis.owner.lstrip("@").lower() if diagnosis.owner else None
    if owner is not None and owner not in owners:
        yield f"owner {diagnosis.owner} is not in may_own"
    if owner == (case.conversation.author or "").lower() and diagnosis.waiting_on == "team":
        yield "the team has the next move, so the owner can't be the author"
    present = owners | {event.who.lower() for event in case.events if event.who is not None}
    for text in _texts(diagnosis):
        for login in MENTION.findall(text):
            if login.lower() not in present:
                yield f"mentions @{login}, who is not in this conversation"


def _texts(diagnosis: Diagnosis) -> list[str]:
    """The model's own words; quotes are checked separately, against their source."""
    return [diagnosis.summary, diagnosis.explanation, diagnosis.next_step, diagnosis.reply or ""]


def _normal(text: str) -> str:
    plain = re.sub(r"\s+", " ", text.translate(_PLAIN_QUOTES)).strip().strip("\"'")
    return plain.removesuffix("...").removesuffix("…").strip().casefold()


def _numbers(text: str) -> list[float]:
    return [float(match) for match in _NUMBER.findall(text.replace(",", ""))]


def _allowed_numbers(case: CaseFile) -> set[float]:
    """The computed facts, plus any number written in the thread (versions, issue numbers)."""
    allowed: set[float] = {0.0, 1.0, 2.0, float(case.number)}
    texts: list[str] = [case.conversation.title, *case.quotable().values()]
    for value in _flatten(case.facts.values()):
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            allowed.add(float(value))
        elif isinstance(value, str):
            texts.append(value)
    for text in texts:
        allowed.update(_numbers(text))
    allowed.update({float(round(value)) for value in allowed})
    return allowed


def _flatten(values: Iterable[object]) -> Iterable[object]:
    for value in values:
        if isinstance(value, list):
            yield from value
        else:
            yield value
