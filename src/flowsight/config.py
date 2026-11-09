from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from flowsight.domain import parse_repo_slug


class ConfigError(Exception):
    """The environment is missing or has invalid settings."""


class Settings(BaseSettings):
    """Validated once at startup. Secrets are `SecretStr`, so they never appear in reprs or logs."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    github_token: SecretStr
    port: int = Field(default=8080, ge=1, le=65_535)
    host: str = "127.0.0.1"
    log_level: Literal["critical", "error", "warning", "info", "debug"] = "info"
    database_path: Path = Path("data/flowsight.db")
    # Any OpenAI-compatible chat API. Defaults to Google Gemini's free tier; for a fully local
    # setup point it at Ollama (http://localhost:11434/v1) with any key.
    llm_api_key: SecretStr | None = None
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_model: str = "gemini-3.5-flash-lite"
    # Tried in order when the main model is rate limited or unavailable (comma-separated).
    llm_backup_models: Annotated[tuple[str, ...], NoDecode] = ("gemini-3.5-flash",)
    # Lower-cased owner/name keys; None allows any public repository.
    allowed_repos: Annotated[frozenset[str] | None, NoDecode] = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _lower_case_level(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("llm_backup_models", mode="before")
    @classmethod
    def _parse_model_list(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return tuple(model.strip() for model in value.split(",") if model.strip())

    @field_validator("allowed_repos", mode="before")
    @classmethod
    def _parse_allowed_repos(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        entries = [entry.strip() for entry in value.split(",") if entry.strip()]
        if not entries:
            raise ValueError("must list at least one owner/name")
        invalid = [entry for entry in entries if parse_repo_slug(entry) is None]
        if invalid:
            raise ValueError("entries must be owner/name")
        return frozenset(entry.lower() for entry in entries)


def load_settings(**overrides: object) -> Settings:
    """Loads settings, reporting every problem at once without echoing any value."""
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as error:
        problems = "\n".join(
            f"  - {'.'.join(map(str, issue['loc'])).upper()}: {issue['msg']}"
            for issue in error.errors(include_input=False)
        )
        raise ConfigError(f"Invalid configuration:\n{problems}") from None
