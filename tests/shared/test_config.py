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


def test_public_profile_requires_https_and_turn() -> None:
    settings = Settings(
        deployment_profile="public",
        public_base_url="http://voice.example",
        ice_servers=[],
    )

    assert len(settings.public_profile_errors()) == 2


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

    settings = Settings(
        deployment_profile="public",
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
