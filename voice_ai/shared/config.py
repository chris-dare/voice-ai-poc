from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from voice_ai import __version__


class IceServer(BaseSettings):
    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


class CommonSettings(BaseSettings):
    """Configuration deliberately shared by every independently deployed process."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    app_name: str = "Voice AI"
    log_level: str = "INFO"
    otlp_endpoint: str | None = None
    otlp_metrics_endpoint: str | None = None
    logfire_enabled: bool = False
    logfire_environment: str = "development"
    logfire_capture_content: bool = False


class AuthSettings(CommonSettings):
    """Identity contract shared by the browser gateway and public agent API."""

    api_enabled: bool = Field(
        default=False,
        validation_alias=AliasChoices("API_ENABLED", "PUBLIC_API_ENABLED"),
    )
    api_agent_id: str = Field(
        default="agent_general_assistant",
        validation_alias=AliasChoices("API_AGENT_ID", "PUBLIC_API_AGENT_ID"),
    )
    auth0_domain: str | None = None
    auth0_audience: str | None = None
    auth0_spa_client_id: str | None = None
    auth0_tenant_claim: str = "org_id"
    auth0_default_tenant_id: str | None = None
    auth0_iat_skew_seconds: int = Field(default=30, ge=0, le=300)

    @property
    def auth0_issuer(self) -> str | None:
        if not self.auth0_domain:
            return None
        domain = self.auth0_domain.removeprefix("https://").rstrip("/")
        return f"https://{domain}/"

    @property
    def auth0_jwks_url(self) -> str | None:
        issuer = self.auth0_issuer
        return f"{issuer}.well-known/jwks.json" if issuer else None

    def api_auth_errors(self) -> list[str]:
        if not self.api_enabled:
            return []
        errors: list[str] = []
        if not self.auth0_domain:
            errors.append("AUTH0_DOMAIN is required when API_ENABLED=true")
        if not self.auth0_audience:
            errors.append("AUTH0_AUDIENCE is required when API_ENABLED=true")
        return errors


class AgentSettings(AuthSettings):
    """Configuration owned by the agent API and execution-worker deployment."""

    agent_host: str = "127.0.0.1"
    agent_port: int = 8100
    agent_deployment_profile: Literal["laptop", "public"] = "laptop"
    agent_release_version: str = Field(default=__version__, min_length=1, max_length=255)
    agent_shared_secret: str | None = None
    api_rate_limit_rpm: int = Field(
        default=60,
        ge=1,
        le=10_000,
        validation_alias=AliasChoices("API_RATE_LIMIT_RPM", "PUBLIC_API_RATE_LIMIT_RPM"),
    )
    api_event_retention_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        validation_alias=AliasChoices(
            "API_EVENT_RETENTION_HOURS", "PUBLIC_API_EVENT_RETENTION_HOURS"
        ),
    )
    api_idempotency_retention_hours: int = Field(
        default=24,
        ge=1,
        le=720,
        validation_alias=AliasChoices(
            "API_IDEMPOTENCY_RETENTION_HOURS",
            "PUBLIC_API_IDEMPOTENCY_RETENTION_HOURS",
        ),
    )
    api_max_input_chars: int = Field(
        default=16_000,
        ge=1,
        le=1_000_000,
        validation_alias=AliasChoices("API_MAX_INPUT_CHARS", "PUBLIC_API_MAX_INPUT_CHARS"),
    )
    api_max_request_body_bytes: int = Field(default=1_048_576, ge=1_024, le=100_000_000)
    confirmation_ttl_seconds: int = Field(default=300, ge=30, le=3_600)
    agent_model: str = ""
    agent_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    agent_fallback_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    agent_model_probe_ttl_seconds: int = Field(default=60, ge=5, le=3_600)
    agent_subagent_model: str | None = None
    agent_gateway_base_url: str | None = None
    agent_gateway_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    agent_thinking_effort: Literal["off", "minimal", "low", "medium", "high", "xhigh"] = "low"
    agent_deep_thinking_effort: Literal["off", "minimal", "low", "medium", "high", "xhigh"] = (
        "medium"
    )
    agent_max_output_tokens: int = Field(default=2_048, ge=128, le=64_000)
    agent_deep_agents_enabled: bool = True
    agent_planning_enabled: bool = False
    agent_request_limit: int = Field(default=16, ge=1, le=50)
    agent_tool_call_limit: int = Field(default=30, ge=1, le=200)
    agent_total_token_limit: int = Field(default=40_000, ge=512, le=200_000)
    agent_max_output_bytes: int = Field(default=1_048_576, ge=1_024, le=100_000_000)
    agent_subagent_request_limit: int = Field(default=6, ge=1, le=25)
    agent_subagent_tool_call_limit: int = Field(default=8, ge=1, le=50)
    agent_subagent_total_token_limit: int = Field(default=10_000, ge=512, le=100_000)
    agent_slo_queue_delay_ms: int = Field(default=250, ge=1, le=300_000)
    agent_slo_time_to_first_text_ms: int = Field(default=3_000, ge=1, le=300_000)
    agent_slo_completion_ms: int = Field(default=30_000, ge=1, le=900_000)
    agent_eval_judge_model: str | None = None
    agent_embedded_worker: bool = True
    agent_worker_concurrency: int = Field(default=4, ge=1, le=128)
    agent_model_route_concurrency: int = Field(default=8, ge=1, le=10_000)
    agent_tool_route_concurrency: int = Field(default=8, ge=1, le=10_000)
    agent_capacity_wait_seconds: float = Field(default=30, ge=0.1, le=3_600)
    agent_capacity_lease_seconds: int = Field(default=120, ge=15, le=3_600)
    agent_capacity_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    agent_queue_capacity: int = Field(default=1_000, ge=1, le=1_000_000)
    agent_tenant_active_response_limit: int = Field(default=25, ge=1, le=100_000)
    agent_stream_buffer_capacity: int = Field(default=128, ge=1, le=10_000)
    agent_event_broker_capacity: int = Field(default=10_000, ge=1, le=1_000_000)
    agent_execution_timeout_seconds: int = Field(default=300, ge=1, le=86_400)
    agent_worker_presence_ttl_seconds: int = Field(default=30, ge=5, le=300)
    agent_job_max_attempts: int = Field(default=3, ge=1, le=20)
    agent_job_lease_seconds: int = Field(default=90, ge=15, le=3_600)
    agent_job_heartbeat_seconds: int = Field(default=15, ge=1, le=300)
    agent_cancellation_poll_seconds: float = Field(default=1, ge=0.1, le=30)
    agent_job_poll_seconds: float = Field(default=0.25, ge=0.05, le=30)
    api_event_poll_seconds: float = Field(default=0.5, ge=0.05, le=15)
    api_sync_wait_timeout_seconds: int = Field(default=120, ge=1, le=900)
    database_url: str = "postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:55432/voice_ai"
    database_pool_size: int = Field(default=10, ge=1, le=200)
    database_max_overflow: int = Field(default=20, ge=0, le=400)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    ollama_base_url: str = "http://127.0.0.1:11434"
    mcp_config_path: Path | None = None

    @field_validator("mcp_config_path", mode="before")
    @classmethod
    def parse_optional_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("agent_models", "agent_fallback_models", mode="before")
    @classmethod
    def parse_model_list(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    # Some dotenv loaders remove quotes inside JSON-looking
                    # arrays. Treat the remaining bracketed value as CSV.
                    stripped = stripped.removeprefix("[").removesuffix("]")
            return [
                item.strip().strip("'\"")
                for item in stripped.split(",")
                if item.strip().strip("'\"")
            ]
        return value

    @property
    def selectable_model_ids(self) -> tuple[str, ...]:
        """Return the ordered, de-duplicated model allowlist exposed to users."""
        return tuple(
            model_id
            for model_id in dict.fromkeys((self.agent_model, *self.agent_models))
            if model_id
        )

    @property
    def ollama_openai_url(self) -> str:
        return f"{self.ollama_base_url.rstrip('/')}/v1"

    def production_errors(self) -> list[str]:
        if self.agent_deployment_profile != "public":
            return []
        errors = self.api_auth_errors()
        if not self.agent_model:
            errors.append("AGENT_MODEL must name the default configured model")
        if not self.api_enabled:
            errors.append("API_ENABLED must be true in the public agent profile")
        if not self.agent_shared_secret or len(self.agent_shared_secret) < 32:
            errors.append(
                "AGENT_SHARED_SECRET must contain at least 32 characters in the public profile"
            )
        if self.agent_embedded_worker:
            errors.append("AGENT_EMBEDDED_WORKER must be false in the public profile")
        if not self.database_url.startswith("postgresql+asyncpg://"):
            errors.append("DATABASE_URL must use postgresql+asyncpg in the public agent profile")
        return errors


class VoiceSettings(AuthSettings):
    """Configuration owned by the browser and real-time voice gateway."""

    host: str = "0.0.0.0"
    port: int = 7860
    agent_base_url: str = "http://127.0.0.1:8100"
    agent_shared_secret: str | None = None
    whisper_model: str = "base"
    kokoro_voice: str = "af_heart"
    model_cache_dir: Path = Path("models")
    deployment_profile: Literal["laptop", "public"] = "laptop"
    public_base_url: str | None = None
    ice_servers: list[IceServer] = Field(default_factory=list)
    max_concurrent_sessions: int = Field(default=1, ge=1, le=10_000)
    frontend_dist: Path = Path("frontend/dist")

    @field_validator("ice_servers", mode="before")
    @classmethod
    def parse_ice_servers(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                return []
            return json.loads(value)
        return value

    @property
    def kokoro_download_dir(self) -> Path:
        return self.model_cache_dir / "kokoro"

    def public_profile_errors(self) -> list[str]:
        errors: list[str] = []
        if self.deployment_profile == "public":
            if not self.api_enabled:
                errors.append("API_ENABLED must be true in the public voice profile")
            else:
                errors.extend(self.api_auth_errors())
            if not self.auth0_spa_client_id:
                errors.append("AUTH0_SPA_CLIENT_ID is required in the public voice profile")
            if not self.public_base_url or not self.public_base_url.startswith("https://"):
                errors.append("PUBLIC_BASE_URL must be an https:// URL in the public profile")
            has_turn = any(
                any(str(url).startswith(("turn:", "turns:")) for url in _urls(server.urls))
                for server in self.ice_servers
            )
            if not has_turn:
                errors.append("The public profile requires at least one TURN ICE server")
        return errors


class Settings(AgentSettings, VoiceSettings):
    """Compatibility settings for local all-in-one commands and existing callers."""


def _urls(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings()


@lru_cache
def get_voice_settings() -> VoiceSettings:
    return VoiceSettings()
