from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FACTORY_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    # Who does the work: simulated agents, Claude drafts, or the trusted execution gateway.
    mode: Literal["demo", "anthropic", "gateway"] = "demo"
    # Where execution state lives. "postgres" survives restarts and allows several workers.
    runtime: Literal["inprocess", "postgres"] = "inprocess"
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("FACTORY_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", "anthropic_api_key"),
    )
    anthropic_model: Literal["claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"] = (
        "claude-sonnet-5"
    )
    anthropic_max_tokens: int = Field(default=4096, ge=256, le=8192)
    database_url: str = "sqlite:///./data/factory.db"
    # True: the HTTP process also runs requests. False: `python -m factory.worker` does.
    dispatch_enabled: bool = True
    worker_concurrency: int = Field(default=8, ge=1, le=64)
    lease_seconds: int = Field(default=60, ge=5, le=3600)
    agent_timeout_seconds: int = Field(default=900, ge=10, le=14400)
    auth_mode: Literal["demo", "oidc", "google"] = "demo"
    google_audience: str = ""
    google_allowed_emails: str = ""
    google_tenant: str = ""
    public_view: bool = False
    demo_login_enabled: bool = False
    demo_credentials: SecretStr = SecretStr("")
    draft_review_required: bool = False
    trusted_origins: str = ""
    initialize_schema: bool = True
    oidc_issuer: str = ""
    oidc_audience: str = "software-factory"
    oidc_jwks_url: str = ""
    gateway_url: str = ""
    gateway_token: str = ""
    allowed_repositories: str = "platform/customer-portal,platform/order-service,platform/shared-libraries"
    demo_delay: float = 1.2
    intake_per_minute: int = 30

    @model_validator(mode="after")
    def production_guard(self):
        if self.demo_login_enabled:
            import json

            try:
                secret = json.loads(self.demo_credentials.get_secret_value())
                if self.auth_mode != "google" or len(secret["signing_key"]) < 32:
                    raise ValueError()
                if len(bytes.fromhex(secret["salt"])) != 16 or len(bytes.fromhex(secret["password_hash"])) != 32:
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ValueError("Demo accounts require Google auth mode and valid Secret Manager credentials") from None
        if self.public_view and self.auth_mode != "google":
            raise ValueError("Public viewing requires Google authentication for write access")
        if self.auth_mode == "google" and (
            self.mode != "anthropic"
            or not self.google_audience
            or not self.google_allowed_emails
            or not self.google_tenant
        ):
            raise ValueError(
                "Google authentication requires Anthropic draft mode, audience, allowed emails and tenant"
            )
        if self.mode == "anthropic" and not self.anthropic_api_key.get_secret_value().strip():
            raise ValueError("Anthropic mode requires ANTHROPIC_API_KEY; use scripts/start-anthropic.ps1")
        if not self.repositories:
            raise ValueError("Configure at least one allowed repository")
        # Plain HTTP is accepted only for a gateway on this machine.
        local = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")
        if self.mode == "gateway" and not self.gateway_url.startswith(("https://", *local)):
            raise ValueError("Gateway mode requires an HTTPS agent gateway; simulated delivery is disabled")
        if self.mode == "gateway" and self.auth_mode != "oidc":
            raise ValueError("Gateway mode requires OIDC authentication")
        if self.mode == "gateway" and self.runtime != "postgres":
            raise ValueError("Gateway mode requires the durable Postgres runtime (FACTORY_RUNTIME=postgres)")
        if self.runtime == "postgres" and not self.database_url.startswith("postgresql"):
            raise ValueError("The Postgres runtime requires a PostgreSQL FACTORY_DATABASE_URL")
        if self.auth_mode == "oidc" and not (
            self.oidc_issuer.startswith("https://") and self.oidc_jwks_url.startswith("https://")
        ):
            raise ValueError("OIDC requires HTTPS issuer and JWKS URLs")
        return self

    @property
    def repositories(self) -> list[str]:
        return [r.strip() for r in self.allowed_repositories.split(",") if r.strip()]


@lru_cache
def settings() -> Settings:
    return Settings()
