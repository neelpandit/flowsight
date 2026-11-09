import pytest

from flowsight.config import ConfigError, load_settings
from flowsight.domain import RepoRef, parse_repo_slug


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings come only from each test, never from the developer's shell or .env file."""
    names = ("GITHUB_TOKEN", "PORT", "HOST", "LOG_LEVEL", "DATABASE_PATH", "ALLOWED_REPOS",
             "LLM_MODEL", "LLM_BACKUP_MODELS")  # fmt: skip
    for name in names:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir("/")


def test_defaults_apply_when_only_the_token_is_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    settings = load_settings()
    assert (settings.port, settings.host, settings.log_level) == (8080, "127.0.0.1", "info")
    assert settings.allowed_repos is None


def test_blank_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("PORT", "")
    monkeypatch.setenv("ALLOWED_REPOS", "")
    settings = load_settings()
    assert settings.port == 8080
    assert settings.allowed_repos is None


def test_allowlist_is_normalised_to_case_insensitive_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("ALLOWED_REPOS", "WithAstro/Astro, vitejs/vite")
    assert load_settings().allowed_repos == frozenset({"withastro/astro", "vitejs/vite"})


def test_backup_models_are_an_ordered_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    assert load_settings().llm_backup_models == ("gemini-3.5-flash",)
    monkeypatch.setenv("LLM_BACKUP_MODELS", "qwen3:8b, llama3.2")
    assert load_settings().llm_backup_models == ("qwen3:8b", "llama3.2")


def test_every_problem_is_reported_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PORT", "not-a-port")
    monkeypatch.setenv("ALLOWED_REPOS", "astro")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    message = str(caught.value)
    assert "GITHUB_TOKEN" in message
    assert "PORT" in message
    assert "ALLOWED_REPOS" in message


def test_values_are_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "github_pat_do-not-print"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    monkeypatch.setenv("PORT", "0")
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert secret not in str(caught.value)
    assert "github_pat" not in repr(load_settings(port=8080))


@pytest.mark.parametrize(
    "slug", ["withastro/astro", "astral-sh/ruff", "octo-org/.github", "user1/repo.name_x-2"]
)
def test_valid_repository_slugs(slug: str) -> None:
    assert parse_repo_slug(slug) is not None


@pytest.mark.parametrize(
    "slug",
    [
        "", "astro", "a/b/c", "-owner/repo", "owner-/repo", "own--er/repo", "own er/repo",
        "owner/..", "owner/.", "owner/re po", "owner/repo?per_page=100", "owner/repo#x",
        "../../etc/passwd", "owner/%2e%2e", "owner/repo\n", f"{'a' * 40}/repo", f"owner/{'r' * 101}",
    ],
)  # fmt: skip
def test_invalid_repository_slugs(slug: str) -> None:
    assert parse_repo_slug(slug) is None


def test_repository_identity_is_case_insensitive() -> None:
    assert RepoRef("WithAstro", "Astro").key == "withastro/astro"
