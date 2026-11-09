import hashlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter

from flowsight.analytics.metrics import Period
from flowsight.analytics.report import FlowReport, build_flow_report
from flowsight.api.problems import ProblemError, install_problem_handlers
from flowsight.api.rate_limit import RateLimiter
from flowsight.api.schemas import (
    Accepted,
    DigestResponse,
    FlowResponse,
    GeneratedBy,
    HypothesisView,
    InsightResponse,
    JobView,
    SyncStatus,
    TriageResponse,
    WorkMixResponse,
)
from flowsight.api.sync_jobs import Pending, Ready, SyncCoordinator, SyncJob
from flowsight.config import Settings
from flowsight.domain import PullRequest, RepoRef, parse_repo_ref
from flowsight.github.client import GitHubClient
from flowsight.github.errors import GitHubError
from flowsight.github.pull_requests import GraphQLClient
from flowsight.insights.digest import DigestService, to_markdown
from flowsight.insights.llm import InsightModel, OpenAICompatibleModel
from flowsight.insights.service import Explanation, InsightService
from flowsight.store import PullRequestStore, open_database
from flowsight.triage.schema import TriagedPullRequest
from flowsight.triage.service import TriageService
from flowsight.workmix.schema import WorkMix
from flowsight.workmix.service import WorkMixService, WorkMixUnavailableError

DEFAULT_PERIOD = timedelta(days=28)
MAX_PERIOD = timedelta(days=90)
# Syncs reach back at most this far, which bounds how old a baseline can be.
MAX_LOOKBACK = timedelta(days=365)
POLL_AFTER_SECONDS = 10
# Uncached AI answers spend LLM quota, so each client gets a modest budget. One investigation
# in the UI makes four requests (triage, work mix, insights, digest).
AI_REQUESTS_PER_MINUTE = 20

WEB = Path(__file__).parent.parent / "web"
# The UI loads nothing from elsewhere and inserts API data as text, so it can be strict.
# /docs is left out: Swagger UI loads its assets from a CDN.
UI_CSP = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AiServices:
    insights: InsightService
    triage: TriageService
    work_mix: WorkMixService
    digest: DigestService


@dataclass(frozen=True, slots=True)
class Services:
    settings: Settings
    store: PullRequestStore
    sync: SyncCoordinator
    ai: AiServices | None  # None when no LLM is configured
    ai_limiter: RateLimiter
    clock: Callable[[], datetime]


def create_app(
    settings: Settings,
    *,
    github: GraphQLClient | None = None,
    connection: sqlite3.Connection | None = None,
    model: InsightModel | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = connection or open_database(settings.database_path)
        client = github or GitHubClient(settings.github_token.get_secret_value())
        store = PullRequestStore(db)
        coordinator = SyncCoordinator(client, store, clock=clock)
        models = [model] if model else _configured_models(settings)
        ai = (
            AiServices(
                InsightService(models[0], store, backups=models[1:]),
                TriageService(models, store, client),
                WorkMixService(models, store),
                DigestService(models, store),
            )
            if models
            else None
        )
        limiter = RateLimiter(AI_REQUESTS_PER_MINUTE, 60)
        app.state.services = Services(settings, store, coordinator, ai, limiter, clock)
        try:
            yield
        finally:
            await coordinator.close()
            for llm in models:
                if isinstance(llm, OpenAICompatibleModel) and model is None:
                    await llm.aclose()
            if isinstance(client, GitHubClient) and github is None:
                await client.aclose()
            if connection is None:
                db.close()

    app = FastAPI(
        title="Flowsight",
        version="0.1.0",
        summary="Delivery-flow signals and AI root-cause insights for GitHub repositories.",
        lifespan=lifespan,
    )
    install_problem_handlers(app)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers["x-content-type-options"] = "nosniff"
        response.headers["x-frame-options"] = "DENY"
        response.headers["referrer-policy"] = "no-referrer"
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["content-security-policy"] = UI_CSP
        return response

    app.include_router(_routes())
    app.include_router(_ai_routes())
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/", include_in_schema=False)
    async def ui() -> FileResponse:
        return FileResponse(WEB / "index.html")

    return app


def _configured_models(settings: Settings) -> list[InsightModel]:
    """The main model, then its backups, de-duplicated; none without an API key."""
    if settings.llm_api_key is None:
        return []
    names = dict.fromkeys([settings.llm_model, *settings.llm_backup_models])
    return [
        OpenAICompatibleModel(
            api_key=settings.llm_api_key.get_secret_value(),
            base_url=settings.llm_base_url,
            model=name,
        )
        for name in names
    ]


# ---- dependencies --------------------------------------------------------------------------


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(_services)]


def _repo(owner: str, name: str, services: ServicesDep) -> RepoRef:
    repo = parse_repo_ref(owner, name)
    if repo is None:
        raise ProblemError(
            400, "Invalid repository", "Expected a GitHub owner and repository name."
        )
    allowed = services.settings.allowed_repos
    if allowed is not None and repo.key not in allowed:
        raise ProblemError(
            403, "Repository not enabled", "This server only analyses configured repositories."
        )
    return repo


RepoDep = Annotated[RepoRef, Depends(_repo)]


def _period(
    services: ServicesDep,
    start: Annotated[
        datetime | None,
        Query(alias="from", description="Start (inclusive). Default: `to` minus 28 days."),
    ] = None,
    end: Annotated[
        datetime | None,
        Query(alias="to", description="End (exclusive). Default: the start of the current hour."),
    ] = None,
) -> Period:
    """The analysis period. Dates without a time or zone are read as UTC midnight."""
    now = services.clock()
    end = _utc(end) if end else now.replace(minute=0, second=0, microsecond=0)
    start = _utc(start) if start else end - DEFAULT_PERIOD
    if start >= end:
        raise ProblemError(400, "Invalid period", "`from` must be earlier than `to`.")
    if end > now:
        # A period that runs into the future would look like a sudden drop against its baseline.
        raise ProblemError(400, "Invalid period", "`to` cannot be in the future.")
    if end - start > MAX_PERIOD:
        raise ProblemError(400, "Invalid period", f"Periods can be at most {MAX_PERIOD.days} days.")
    if Period(start, end).previous().start < now - MAX_LOOKBACK:
        raise ProblemError(
            400, "Invalid period", "The period and its baseline must fall within the last year."
        )
    return Period(start, end)


PeriodDep = Annotated[Period, Depends(_period)]


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


# ---- helpers -------------------------------------------------------------------------------


def _accepted(repo: RepoRef, job: SyncJob) -> JSONResponse:
    status_url = f"/v1/repos/{repo.owner}/{repo.name}/sync"
    body = Accepted(
        "syncing", f"{repo.owner}/{repo.name}", job.started_at, POLL_AFTER_SECONDS, status_url
    )
    return JSONResponse(
        TypeAdapter(Accepted).dump_python(body, mode="json"),
        status_code=202,
        headers={"retry-after": str(POLL_AFTER_SECONDS), "location": status_url},
    )


def _cached_json(request: Request, body: bytes) -> Response:
    """Short-lived caching with a content hash, so repeat calls can be answered with 304."""
    etag = f'"{hashlib.sha256(body).hexdigest()[:32]}"'
    headers = {"etag": etag, "cache-control": "private, max-age=60"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


def _job_view(job: SyncJob | None) -> JobView | None:
    if job is None:
        return None
    error = "Sync failed; see server logs." if job.error else None
    return JobView(job.status, job.started_at, job.finished_at, error)


def _ai(request: Request, services: Services) -> AiServices:
    """The AI services: a 503 without a configured model, a 429 once this client's budget is
    spent."""
    if services.ai is None:
        raise ProblemError(
            503, "AI insights not configured", "Set LLM_API_KEY to enable this endpoint."
        )
    client = request.client.host if request.client else "unknown"
    wait = services.ai_limiter.retry_after(client)
    if wait is not None:
        raise ProblemError(
            429, "Too many requests", "AI endpoints are rate limited.", {"retry-after": str(wait)}
        )
    return services.ai


@dataclass(frozen=True, slots=True)
class _Loaded:
    readiness: Ready
    name: str  # owner/name as GitHub spells it
    pull_requests: list[PullRequest]  # covering the period and its baseline


async def _load(services: Services, repo: RepoRef, period: Period) -> _Loaded | JSONResponse:
    """Stored pull requests for the period and its baseline, or a 202 while a sync runs."""
    readiness = await services.sync.ensure(repo, period.previous().start)
    if isinstance(readiness, Pending):
        return _accepted(repo, readiness.job)
    name = services.store.name_with_owner(repo.key) or repo.key
    pull_requests = await run_in_threadpool(
        services.store.pull_requests_since, repo.key, period.previous().start
    )
    return _Loaded(readiness, name, pull_requests)


@dataclass(frozen=True, slots=True)
class _Investigation:
    report: FlowReport
    triaged: list[TriagedPullRequest]
    work_mix: WorkMix | None
    explanation: Explanation


async def _investigate(
    ai: AiServices, repo: RepoRef, loaded: _Loaded, period: Period, now: datetime
) -> _Investigation:
    """Everything the explanation draws on. Reading conversations or labelling work can fail
    (GitHub trouble, no model available); the explanation then rests on what remains."""
    report = await run_in_threadpool(build_flow_report, loaded.pull_requests, period)
    try:
        triage = await ai.triage.triage(repo, loaded.name, loaded.pull_requests, period, now)
        triaged = triage.pull_requests
    except GitHubError as error:
        logger.warning("could not read conversations, explaining without them: %s", error)
        triaged = []
    try:
        work_mix = await ai.work_mix.work_mix(repo.key, loaded.name, loaded.pull_requests, period)
    except WorkMixUnavailableError as error:
        logger.warning("could not label the work, explaining without it: %s", error)
        work_mix = None
    explanation = await ai.insights.explain(loaded.name, report, report.flows, triaged, work_mix)
    return _Investigation(report, triaged, work_mix, explanation)


def _insight_response(
    repository: str, readiness: Ready, period: Period, baseline: Period, explanation: Explanation
) -> InsightResponse:
    insight = explanation.insight
    hypothesis = insight.hypothesis
    return InsightResponse(
        repository=repository,
        data_as_of=readiness.data_as_of,
        period=period,
        baseline_period=baseline,
        generated_by=GeneratedBy(
            explanation.generator, explanation.model, explanation.prompt_version, explanation.cached
        ),
        headline=insight.headline,
        summary=insight.summary,
        changes=insight.changes,
        findings=insight.findings,
        hypothesis=HypothesisView(
            hypothesis.statement,
            hypothesis.cause,
            hypothesis.reasoning,
            explanation.confidence,
            hypothesis.evidence,
            hypothesis.alternatives_considered,
            hypothesis.how_to_verify,
        ),
        recommended_actions=insight.recommended_actions,
        caveats=insight.caveats,
        evidence=explanation.evidence,
        rejected_because=explanation.rejected_because,
    )


_FLOW = TypeAdapter(FlowResponse)
_INSIGHT = TypeAdapter(InsightResponse)
_TRIAGE = TypeAdapter(TriageResponse)
_WORK_MIX = TypeAdapter(WorkMixResponse)
_DIGEST = TypeAdapter(DigestResponse)

# ---- routes --------------------------------------------------------------------------------

_SYNCING: dict[int | str, dict[str, Any]] = {
    202: {"model": Accepted, "description": "First request for this data: a sync has started."}
}


def _routes() -> APIRouter:
    router = APIRouter()
    syncing = _SYNCING

    @router.get("/v1/repos/{owner}/{name}/flow", response_model=FlowResponse, responses=syncing)
    async def flow(
        request: Request, repo: RepoDep, period: PeriodDep, services: ServicesDep
    ) -> Response:
        """Review flow for a period compared with the equally long period before it: velocity,
        stage times, reviewer load, and the changes that are statistically real (signals)."""
        readiness = await services.sync.ensure(repo, period.previous().start)
        if isinstance(readiness, Pending):
            return _accepted(repo, readiness.job)
        name = services.store.name_with_owner(repo.key) or repo.key

        def build() -> bytes:
            pull_requests = services.store.pull_requests_since(repo.key, period.previous().start)
            report = build_flow_report(pull_requests, period)
            response = FlowResponse(
                name, readiness.data_as_of, readiness.refreshing, report.period,
                report.baseline_period, report.current, report.baseline, report.signals,
            )  # fmt: skip
            return _FLOW.dump_json(response)

        # Statistics are CPU work; keep them off the event loop.
        return _cached_json(request, await run_in_threadpool(build))

    @router.get("/v1/repos/{owner}/{name}/sync", response_model=SyncStatus)
    async def sync_status(repo: RepoDep, services: ServicesDep) -> SyncStatus:
        """How far back stored data reaches, and the state of the latest background sync."""
        state = services.store.sync_state(repo.key)
        job = services.sync.job(repo)
        if state is None and job is None:
            raise ProblemError(404, "Not synced", "This repository has not been synced yet.")
        return SyncStatus(
            services.store.name_with_owner(repo.key) or repo.key,
            state.covered_from if state else None,
            state.synced_at if state else None,
            _job_view(job),
        )

    @router.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return router


def _ai_routes() -> APIRouter:
    """The endpoints that ask a model. Each answer is checked in code before it is returned,
    and cached, so repeating a request costs no tokens."""
    router = APIRouter()

    @router.get(
        "/v1/repos/{owner}/{name}/triage", response_model=TriageResponse, responses=_SYNCING
    )
    async def triage(
        request: Request, repo: RepoDep, period: PeriodDep, services: ServicesDep
    ) -> Response:
        """The AI's reading of the still-open pull requests that became ready in the period and
        waited longest. For each: what it changes and its value, what is in its way, the action
        to take, its owner, a drafted reply, and quotes checked verbatim against GitHub."""
        ai = _ai(request, services)
        loaded = await _load(services, repo, period)
        if isinstance(loaded, JSONResponse):
            return loaded
        result = await ai.triage.triage(
            repo, loaded.name, loaded.pull_requests, period, services.clock()
        )
        response = TriageResponse(
            loaded.name, loaded.readiness.data_as_of, period, result.models,
            result.prompt_version, result.cached, result.pull_requests, result.not_shown,
        )  # fmt: skip
        return _cached_json(request, _TRIAGE.dump_json(response))

    @router.get(
        "/v1/repos/{owner}/{name}/work-mix", response_model=WorkMixResponse, responses=_SYNCING
    )
    async def work_mix(
        request: Request, repo: RepoDep, period: PeriodDep, services: ServicesDep
    ) -> Response:
        """Where the work goes: every human pull request merged in the period and the one
        before, labelled by a model with its type, area and risk, then counted in code: volume
        and first-review wait by type and area, and merges without review that deserve a
        second look."""
        ai = _ai(request, services)
        loaded = await _load(services, repo, period)
        if isinstance(loaded, JSONResponse):
            return loaded
        try:
            mix = await ai.work_mix.work_mix(repo.key, loaded.name, loaded.pull_requests, period)
        except WorkMixUnavailableError:
            raise ProblemError(
                503, "AI unavailable", "No model could label the work; try again shortly.",
                {"retry-after": "60"},
            ) from None  # fmt: skip
        response = WorkMixResponse(
            loaded.name, loaded.readiness.data_as_of, period, period.previous(), mix
        )
        return _cached_json(request, _WORK_MIX.dump_json(response))

    @router.get(
        "/v1/repos/{owner}/{name}/insights", response_model=InsightResponse, responses=_SYNCING
    )
    async def insights(
        request: Request, repo: RepoDep, period: PeriodDep, services: ServicesDep
    ) -> Response:
        """An LLM's explanation of the period: what is going on and why, the most likely root
        cause with a confidence score and a way to verify it, and actions with owners. It draws
        on the statistics, the readings of conversations (`/triage`) and the work mix
        (`/work-mix`). Every claim cites evidence IDs that resolve to items in `evidence`;
        answers that fail those checks are never returned."""
        ai = _ai(request, services)
        loaded = await _load(services, repo, period)
        if isinstance(loaded, JSONResponse):
            return loaded
        found = await _investigate(ai, repo, loaded, period, services.clock())
        response = _insight_response(
            loaded.name, loaded.readiness, found.report.period, found.report.baseline_period,
            found.explanation,
        )  # fmt: skip
        return _cached_json(request, _INSIGHT.dump_json(response))

    @router.get(
        "/v1/repos/{owner}/{name}/digest", response_model=DigestResponse, responses=_SYNCING
    )
    async def digest(
        request: Request, repo: RepoDep, period: PeriodDep, services: ServicesDep
    ) -> Response:
        """A team digest, ready to post: what shipped, what needs someone and who, and what
        needs deciding. It is written from the other AI endpoints' checked results, and checked
        again: every pull request, person and number must come from them. Also returned as
        Markdown."""
        ai = _ai(request, services)
        loaded = await _load(services, repo, period)
        if isinstance(loaded, JSONResponse):
            return loaded
        found = await _investigate(ai, repo, loaded, period, services.clock())
        explanation = found.explanation
        result = await ai.digest.digest(
            loaded.name, found.report, found.work_mix, found.triaged,
            explanation.insight if explanation.generator == "llm" else None,
        )  # fmt: skip
        response = DigestResponse(
            loaded.name, period, result.model, result.prompt_version, result.cached,
            result.digest,
            to_markdown(result.digest, loaded.name, period) if result.digest else None,
            result.rejected_because,
        )  # fmt: skip
        return _cached_json(request, _DIGEST.dump_json(response))

    return router
