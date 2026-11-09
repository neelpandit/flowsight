import json
from typing import Any

from fastapi.testclient import TestClient

from flowsight.analytics.report import build_flow_report
from flowsight.api.app import create_app
from flowsight.insights.digest import (
    Digest,
    DigestPoint,
    DigestService,
    digest_material,
    to_markdown,
    validate_digest,
)
from flowsight.insights.llm import LlmError
from flowsight.store import PullRequestStore, open_database
from flowsight.workmix.schema import LabelAnswer, TaxonomyAnswer
from tests.fakes import FakeGitHub, ScriptedModel
from tests.test_api import NOW, settings
from tests.test_workmix import AREAS, LABELS, PERIOD, PULL_REQUESTS, mix, synced_store


def material() -> tuple[str, dict[str, object]]:
    report = build_flow_report(PULL_REQUESTS, PERIOD)
    found = digest_material("acme/widgets", report, mix(), [], None)
    return json.dumps(found, indent=0, default=str), found


def digest(**overrides: Any) -> Digest:
    fields: dict[str, Any] = {
        "title": "Routing fixes shipped; one risky merge to check",
        "tldr": "We merged 5 pull requests. Please look at the token change.",
        "shipped": [DigestPoint(text="Scroll position is kept when navigating.", pull_requests=[101])],
        "needs_attention": [DigestPoint(text="Review the token refresh change.", pull_requests=[104])],
        "decisions": [],
    }  # fmt: skip
    return Digest(**(fields | overrides))


def test_a_digest_drawn_from_its_material_passes() -> None:
    prompt, found = material()
    assert validate_digest(digest(), prompt, found) == []


def test_pull_requests_people_and_numbers_must_come_from_the_material() -> None:
    prompt, found = material()
    made_up = digest(
        tldr="We merged 12 pull requests; @mallory should review #999.",
        shipped=[DigestPoint(text="Faster builds.", pull_requests=[101, 777])],
    )
    assert validate_digest(made_up, prompt, found) == [
        "@mallory does not appear in its material",
        "cites pull requests it was not given: [777]",
        "mentions #999, which it was not given",
        "number 12 does not appear in its material",
    ]


def test_markdown_links_every_pull_request() -> None:
    text = to_markdown(digest(), "acme/widgets", PERIOD)
    assert "**Shipped**" in text
    assert "[#104](https://github.com/acme/widgets/pull/104)" in text
    assert "To decide" not in text  # empty sections are left out


async def test_a_digest_is_checked_cached_and_absent_when_no_model_answers() -> None:
    report = build_flow_report(PULL_REQUESTS, PERIOD)
    store = PullRequestStore(open_database(":memory:"))
    bad = digest(tldr="We merged 12 pull requests.")
    model = ScriptedModel(bad, digest())
    service = DigestService([model], store)
    first = await service.digest("acme/widgets", report, mix(), [], None)
    second = await service.digest("acme/widgets", report, mix(), [], None)
    assert (first.digest, first.cached, second.cached) == (digest(), False, True)
    assert "number 12 does not appear" in model.prompts[1]

    down = DigestService([ScriptedModel(LlmError("RateLimitError"))], store)
    other = await down.digest("acme/widgets", report, None, [], None)
    assert other.digest is None
    assert "RateLimitError" in other.rejected_because[0]


def test_the_endpoint_returns_the_digest_as_markdown() -> None:
    model = ScriptedModel(TaxonomyAnswer(areas=AREAS), LabelAnswer(labels=LABELS), digest())
    app = create_app(
        settings(), github=FakeGitHub([]), connection=synced_store()._db, model=model,
        clock=lambda: NOW,
    )  # fmt: skip
    with TestClient(app) as client:
        body = client.get("/v1/repos/acme/widgets/digest").json()
    assert body["digest"]["title"] == digest().title
    assert body["markdown"].startswith(f"**{digest().title}**")
