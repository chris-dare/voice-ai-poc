import json

from voice_ai.shared.config import AgentSettings, Settings, VoiceSettings


def test_deployed_services_only_load_their_owned_configuration() -> None:
    assert "agent_model" in AgentSettings.model_fields
    assert "database_url" in AgentSettings.model_fields
    assert "openrouter_api_key" in AgentSettings.model_fields
    assert "whisper_model" not in AgentSettings.model_fields
    assert "kokoro_voice" not in AgentSettings.model_fields

    assert "whisper_model" in VoiceSettings.model_fields
    assert "kokoro_voice" in VoiceSettings.model_fields
    assert "agent_model" not in VoiceSettings.model_fields
    assert "database_url" not in VoiceSettings.model_fields


def test_public_agent_profile_rejects_unsafe_process_configuration() -> None:
    unsafe = AgentSettings(
        _env_file=None,
        agent_deployment_profile="public",
        api_enabled=False,
        agent_shared_secret="short",
        agent_embedded_worker=True,
    )
    errors = unsafe.production_errors()
    assert any("API_ENABLED" in error for error in errors)
    assert any("32 characters" in error for error in errors)
    assert any("AGENT_EMBEDDED_WORKER" in error for error in errors)


def test_public_agent_profile_accepts_only_the_distributed_database_configuration() -> None:
    safe = AgentSettings(
        _env_file=None,
        agent_deployment_profile="public",
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://agent.example.com",
        agent_model="openrouter:openai/example",
        agent_shared_secret="a" * 32,
        agent_embedded_worker=False,
        database_url="postgresql+asyncpg://agent:secret@postgres:5432/agent",
    )
    unsafe = safe.model_copy(
        update={"database_url": "sqlite+aiosqlite:///accidental-production.db"}
    )

    assert safe.production_errors() == []
    assert any("postgresql+asyncpg" in error for error in unsafe.production_errors())


def test_public_profile_requires_https_and_turn() -> None:
    settings = VoiceSettings(
        _env_file=None,
        deployment_profile="public",
        public_base_url="http://voice.example",
        ice_servers=[],
    )

    errors = settings.public_profile_errors()
    assert any("API_ENABLED" in error for error in errors)
    assert any("AUTH0_SPA_CLIENT_ID" in error for error in errors)
    assert any("https://" in error for error in errors)
    assert any("TURN" in error for error in errors)


def test_ice_servers_parse_from_environment_shape() -> None:
    raw = json.dumps(
        [
            {
                "urls": ["stun:stun.example:3478", "turns:turn.example:443"],
                "username": "demo",
                "credential": "secret",
            }
        ]
    )

    settings = VoiceSettings(
        _env_file=None,
        deployment_profile="public",
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://agent.example.com",
        auth0_spa_client_id="spa-client-id",
        public_base_url="https://voice.example",
        ice_servers=raw,
    )

    assert settings.public_profile_errors() == []


def test_agent_api_requires_auth0_configuration_only_when_enabled() -> None:
    assert Settings(api_enabled=False).api_auth_errors() == []

    missing = Settings(
        api_enabled=True,
        auth0_domain=None,
        auth0_audience=None,
    ).api_auth_errors()
    assert "AUTH0_DOMAIN is required when API_ENABLED=true" in missing
    assert "AUTH0_AUDIENCE is required when API_ENABLED=true" in missing

    configured = Settings(
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://agent-api.example.com",
    )
    assert configured.api_auth_errors() == []
    assert configured.auth0_issuer == "https://tenant.example.auth0.com/"
    assert configured.auth0_jwks_url == ("https://tenant.example.auth0.com/.well-known/jwks.json")


def test_logfire_content_capture_is_private_by_default() -> None:
    settings = Settings(_env_file=None)

    assert not settings.logfire_enabled
    assert not settings.logfire_capture_content
    assert settings.logfire_environment == "development"


def test_agent_process_memory_and_tenant_capacity_are_bounded_by_default() -> None:
    settings = AgentSettings(_env_file=None)

    assert settings.agent_tenant_active_response_limit > 0
    assert settings.agent_stream_buffer_capacity > 0
    assert settings.agent_event_broker_capacity > 0
    assert settings.agent_max_output_bytes > 0
    assert settings.agent_model_route_concurrency > 0
    assert settings.agent_tool_route_concurrency > 0
    assert settings.agent_capacity_wait_seconds > 0
    assert settings.agent_release_version


def test_auth0_iat_skew_is_narrow_and_configurable() -> None:
    assert Settings(_env_file=None).auth0_iat_skew_seconds == 30
    assert Settings(_env_file=None, auth0_iat_skew_seconds=5).auth0_iat_skew_seconds == 5


def test_agent_model_fallbacks_accept_json_or_comma_separated_env_shapes() -> None:
    assert Settings(
        _env_file=None,
        agent_fallback_models='["openai:gpt-5", "ollama:qwen3:1.7b"]',
    ).agent_fallback_models == ["openai:gpt-5", "ollama:qwen3:1.7b"]


def test_selectable_models_are_configuration_only_and_deduplicated() -> None:
    settings = AgentSettings(
        _env_file=None,
        agent_model="provider:default",
        agent_models="provider:alternate, provider:default",
    )

    assert settings.selectable_model_ids == (
        "provider:default",
        "provider:alternate",
    )
    assert Settings(
        _env_file=None,
        agent_fallback_models="openai:gpt-5, ollama:qwen3:1.7b",
    ).agent_fallback_models == ["openai:gpt-5", "ollama:qwen3:1.7b"]


def test_model_catalog_accepts_dotenv_dequoted_array(monkeypatch) -> None:
    monkeypatch.setenv(
        "AGENT_MODELS",
        "[provider:first,provider:second]",
    )

    settings = AgentSettings(_env_file=None, agent_model="provider:default")

    assert settings.agent_models == ["provider:first", "provider:second"]
