from flowsight.domain import RepoRef


class GitHubError(Exception):
    pass


class GitHubAuthError(GitHubError):
    """The token was rejected. Not retryable: the configuration needs fixing."""


class GitHubRateLimitError(GitHubError):
    """GitHub asked us to slow down for longer than we are willing to wait in-process."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(f"GitHub rate limit reached; retry in {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class GitHubUnavailableError(GitHubError):
    """GitHub kept failing (5xx, timeouts, network errors) after retries."""


class GitHubResponseError(GitHubError):
    """GitHub answered, but not with what was asked for: GraphQL errors or an unexpected shape."""


class RepositoryNotFoundError(Exception):
    """The repository does not exist or is not public; deliberately indistinguishable."""

    def __init__(self, repo: RepoRef) -> None:
        super().__init__(f"Repository {repo.owner}/{repo.name} was not found or is not public")
        self.repo = repo
