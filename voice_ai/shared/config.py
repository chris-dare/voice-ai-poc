from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class IceServer(BaseSettings):
    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    app_name: str = "Voice AI"
    host: str = "0.0.0.0"
    port: int = 7860
    agent_host: str = "127.0.0.1"
    agent_port: int = 8100
    agent_base_url: str = "http://127.0.0.1:8100"
    agent_shared_secret: str | None = None
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
    confirmation_ttl_seconds: int = Field(default=300, ge=30, le=3_600)
    agent_model: str = "anthropic:claude-sonnet-4-6"
    agent_fallback_models: list[str] = Field(default_factory=list)
    agent_subagent_model: str | None = None
    agent_gateway_base_url: str | None = None
    agent_gateway_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    agent_thinking_effort: Literal["off", "minimal", "low", "medium", "high", "xhigh"] = (
        "high"
    )
    agent_max_output_tokens: int = Field(default=2_048, ge=128, le=64_000)
    agent_deep_agents_enabled: bool = True
    agent_request_limit: int = Field(default=16, ge=1, le=50)
    agent_tool_call_limit: int = Field(default=30, ge=1, le=200)
    agent_total_token_limit: int = Field(default=40_000, ge=512, le=200_000)
    agent_slo_queue_delay_ms: int = Field(default=250, ge=1, le=300_000)
    agent_slo_time_to_first_text_ms: int = Field(default=3_000, ge=1, le=300_000)
    agent_slo_completion_ms: int = Field(default=30_000, ge=1, le=900_000)
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://voice_ai:voice_ai@127.0.0.1:55432/voice_ai"
    ollama_base_url: str = "http://127.0.0.1:11434"
    whisper_model: str = "base"
    tts_provider: Literal["kokoro", "piper"] = "kokoro"
    kokoro_voice: str = "af_heart"
    piper_voice: str = "en_US-lessac-medium"
    model_cache_dir: Path = Path("models")
    mcp_config_path: Path | None = None
    deployment_profile: Literal["laptop", "public"] = "laptop"
    public_base_url: str | None = None
    ice_servers: list[IceServer] = Field(default_factory=list)
    max_concurrent_sessions: int = Field(default=1, ge=1, le=16)
    otlp_endpoint: str | None = None
    logfire_enabled: bool = False
    logfire_environment: str = "development"
    logfire_capture_content: bool = False
    frontend_dist: Path = Path("frontend/dist")

    @field_validator("ice_servers", mode="before")
    @classmethod
    def parse_ice_servers(cls, value: object) -> object:
        if isinstance(value, str):
            if not value.strip():
                return []
            return json.loads(value)
        return value

    @field_validator("mcp_config_path", mode="before")
    @classmethod
    def parse_optional_path(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("agent_fallback_models", mode="before")
    @classmethod
    def parse_model_list(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return json.loads(stripped)
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @property
    def ollama_openai_url(self) -> str:
        return f"{self.ollama_base_url.rstrip('/')}/v1"

    @property
    def piper_download_dir(self) -> Path:
        return self.model_cache_dir / "piper"

    @property
    def kokoro_download_dir(self) -> Path:
        return self.model_cache_dir / "kokoro"

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

    def public_profile_errors(self) -> list[str]:
        errors: list[str] = []
        if self.deployment_profile == "public":
            if not self.public_base_url or not self.public_base_url.startswith("https://"):
                errors.append("PUBLIC_BASE_URL must be an https:// URL in the public profile")
            has_turn = any(
                any(str(url).startswith(("turn:", "turns:")) for url in _urls(server.urls))
                for server in self.ice_servers
            )
            if not has_turn:
                errors.append("The public profile requires at least one TURN ICE server")
        return errors


def _urls(value: str | list[str]) -> list[str]:
    return [value] if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
