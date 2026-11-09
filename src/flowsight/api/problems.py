"""Errors as RFC 9457 problem details (`application/problem+json`)."""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from flowsight.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubRateLimitError,
    RepositoryNotFoundError,
)

logger = logging.getLogger(__name__)


class ProblemError(Exception):
    def __init__(
        self, status: int, title: str, detail: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.headers = headers


def problem(
    status: int, title: str, detail: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        {"type": "about:blank", "title": title, "status": status, "detail": detail},
        status_code=status,
        headers=headers,
        media_type="application/problem+json",
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(_: Request, error: ProblemError) -> JSONResponse:
        return problem(error.status, error.title, error.detail, error.headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, error: RequestValidationError) -> JSONResponse:
        # Name the offending parameters without echoing their values back.
        issues = "; ".join(
            f"{'.'.join(map(str, issue['loc'][1:]))}: {issue['msg']}" for issue in error.errors()
        )
        return problem(400, "Invalid request", issues)

    @app.exception_handler(RepositoryNotFoundError)
    async def _not_found(_: Request, error: RepositoryNotFoundError) -> JSONResponse:
        return problem(404, "Repository not found", str(error))

    @app.exception_handler(GitHubError)
    async def _upstream(_: Request, error: GitHubError) -> JSONResponse:
        if isinstance(error, GitHubRateLimitError):
            return problem(
                503,
                "GitHub rate limit reached",
                "GitHub asked us to slow down; try again later.",
                headers={"retry-after": str(error.retry_after_seconds)},
            )
        if isinstance(error, GitHubAuthError):
            logger.error("GitHub rejected the configured token")
            return problem(
                502, "Upstream authentication failed", "GitHub rejected the service's token."
            )
        logger.warning("GitHub request failed: %s", error)
        return problem(
            502, "GitHub request failed", "GitHub could not be reached or answered unexpectedly."
        )
