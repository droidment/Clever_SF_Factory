import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# First-party rates per million tokens: input, output, cache read. Cache writes cost 1.25x input.
# Checked 2026-11-18. A model missing from this table cannot be selected, so spend is never guessed.
RATES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
}


class Repository(BaseModel):
    """One repository the gateway may work on. Only an administrator edits this file."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1)
    base: str = "main"
    # Run inside the sandbox at the integrated commit. Empty means the test step fails closed.
    test_command: str = ""
    scan_command: str = ""
    image: str = "python:3.13-slim"
    # "owner/name" on GitHub. With a token, an approved release opens a pull request there.
    github: str = Field(default="", pattern=r"^$|^[\w.-]+/[\w.-]+$")
    # Agents can never write these, whatever the request says.
    protected_paths: list[str] = [".github/*"]
    # The only paths the documentation agent may write.
    documentation_paths: list[str] = ["*.md", "*.rst", "docs/*"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    # Shared secret the factory presents as a bearer token.
    token: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("GATEWAY_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "anthropic_api_key"),
    )
    model: Literal["claude-opus-5-5", "claude-sonnet-5-5"] = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_turns: int = Field(default=60, ge=2, le=400)
    database_url: str = "sqlite:///./data/gateway.db"
    workspace_root: str = "./data/workspaces"
    repositories_file: str = "gateway.repositories.json"
    # "docker": no network, throwaway container. "process": the host, for trusted repositories only.
    sandbox: Literal["docker", "process"] = "docker"
    command_timeout_seconds: int = Field(default=900, ge=5, le=7200)
    operation_timeout_seconds: int = Field(default=1800, ge=10, le=14400)
    # How long one HTTP call waits for a result before answering 202 "still running".
    wait_seconds: float = Field(default=100, ge=0, le=150)
    github_token: SecretStr = SecretStr("")
    github_api: str = "https://api.github.com"

    @model_validator(mode="after")
    def guard(self):
        if len(self.token.get_secret_value()) < 32:
            raise ValueError("GATEWAY_TOKEN must be a random secret of at least 32 characters")
        return self

    @property
    def repositories(self) -> dict[str, Repository]:
        return load_repositories(str(Path(self.repositories_file).resolve()))


@lru_cache
def load_repositories(path: str) -> dict[str, Repository]:
    with open(path, encoding="utf-8") as handle:
        return {name: Repository.model_validate(value) for name, value in json.load(handle).items()}


@lru_cache
def settings() -> Settings:
    return Settings()
