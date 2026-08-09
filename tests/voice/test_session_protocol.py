from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from voice_ai.shared.config import Settings
from voice_ai.voice.app import SessionCapacity, _run_session_lazy, create_app
from voice_ai.voice.health import CheckResult
from voice_ai.voice.runtime import run_session


@pytest.mark.asyncio
async def test_voice_session_capacity_emits_admission_and_saturation_metrics(monkeypatch) -> None:
    metric = Mock()
    monkeypatch.setattr("voice_ai.voice.app.record_voice_session", metric)
    capacity = SessionCapacity(1)

    assert await capacity.reserve()
    assert not await capacity.reserve()
    await capacity.release()

    assert [call.kwargs for call in metric.call_args_list] == [
        {"event": "accepted", "active": 1},
        {"event": "rejected", "active": 1},
        {"event": "released", "active": 0},
    ]


@pytest.mark.asyncio
async def test_lazy_voice_runtime_failure_releases_reserved_capacity(monkeypatch) -> None:
    capacity = SessionCapacity(1)
    assert await capacity.reserve()

    def fail_import(_name: str):
        raise ImportError("voice runtime unavailable")

    monkeypatch.setattr("voice_ai.voice.app.importlib.import_module", fail_import)

    with pytest.raises(ImportError, match="voice runtime unavailable"):
        await _run_session_lazy(
            connection=Mock(),
            settings=Mock(),
            capacity=capacity,
        )

    assert capacity.active == 0


@pytest.mark.asyncio
async def test_session_ready_status_uses_rtvi_server_message(monkeypatch) -> None:
    runner = Mock()
    runner.add_workers = AsyncMock()

    async def run_forever() -> None:
        return None

    runner.run = run_forever
    monkeypatch.setattr("voice_ai.voice.runtime.WorkerRunner", lambda **_kwargs: runner)

    rtvi = Mock()
    rtvi.send_server_message = AsyncMock()
    session = Mock(rtvi=rtvi, worker=Mock(), tts=Mock())
    monkeypatch.setattr("voice_ai.voice.runtime.create_voice_session", lambda **_kwargs: session)
    monkeypatch.setattr("voice_ai.voice.runtime.warm_tts_service", AsyncMock(return_value=0.01))

    connection = Mock()
    connection.send_app_message = Mock()
    capacity = SessionCapacity(1)
    capacity.active = 1

    await run_session(
        connection=connection,
        settings=Mock(),
        capacity=capacity,
    )

    assert rtvi.send_server_message.await_args_list == [
        (
            (
                {
                    "type": "session-status",
                    "data": {
                        "state": "ready",
                        "detail": "Speech and agent services ready",
                    },
                },
            ),
            {},
        ),
    ]
    connection.send_app_message.assert_not_called()
    assert capacity.active == 0


@pytest.mark.asyncio
async def test_browser_config_exposes_only_public_auth_values(tmp_path) -> None:
    settings = Settings(
        api_enabled=True,
        auth0_domain="tenant.example.auth0.com",
        auth0_audience="https://agent.example.com",
        auth0_spa_client_id="spa-client-id",
        agent_shared_secret="must-not-leak",
        frontend_dist=tmp_path,
    )
    app = create_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/config")

    assert response.status_code == 200
    assert response.json() == {
        "agent_id": "agent_general_assistant",
        "agent_api_base": "/agent-api/v1",
        "auth": {
            "enabled": True,
            "domain": "tenant.example.auth0.com",
            "client_id": "spa-client-id",
            "audience": "https://agent.example.com",
        },
    }
    assert "must-not-leak" not in response.text


@pytest.mark.asyncio
async def test_gateway_rejects_chunked_body_over_its_raw_request_limit(tmp_path) -> None:
    app = create_app(
        Settings(
            frontend_dist=tmp_path,
            voice_max_request_body_bytes=1_024,
        )
    )

    async def oversized_body():
        yield b"{" + (b'"padding":"' + (b"x" * 1_024))
        yield b'"}'

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/telemetry",
            headers={"Content-Type": "application/json"},
            content=oversized_body(),
        )

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "request_body_too_large"


@pytest.mark.asyncio
async def test_health_does_not_expose_turn_credentials(tmp_path) -> None:
    settings = Settings(
        frontend_dist=tmp_path,
        ice_servers=[
            {
                "urls": "turn:turn.example.com:3478",
                "username": "turn-user-must-not-leak",
                "credential": "turn-secret-must-not-leak",
            }
        ],
    )
    app = create_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")

    assert response.status_code == 503
    assert response.json()["ice_server_count"] == 1
    assert "turn-user-must-not-leak" not in response.text
    assert "turn-secret-must-not-leak" not in response.text


@pytest.mark.asyncio
async def test_voice_config_requires_and_validates_authentication(tmp_path, monkeypatch) -> None:
    validate = AsyncMock()
    monkeypatch.setattr("voice_ai.voice.app._validate_public_access_token", validate)
    app = create_app(
        Settings(
            api_enabled=True,
            auth0_domain="tenant.example.auth0.com",
            auth0_audience="https://agent.example.com",
            auth0_spa_client_id="spa-client-id",
            frontend_dist=tmp_path,
            ice_servers=[
                {
                    "urls": "turn:turn.example.com:3478",
                    "username": "turn-user",
                    "credential": "turn-secret",
                }
            ],
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        unauthenticated = await client.get("/api/voice-config")
        authenticated = await client.get(
            "/api/voice-config",
            headers={"Authorization": "Bearer user-token"},
        )

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json() == {
        "ice_servers": [
            {
                "urls": "turn:turn.example.com:3478",
                "username": "turn-user",
                "credential": "turn-secret",
            }
        ]
    }
    validate.assert_awaited_once()
    assert validate.await_args.kwargs["access_token"] == "user-token"


@pytest.mark.asyncio
async def test_conversation_deep_link_serves_spa_entrypoint(tmp_path) -> None:
    (tmp_path / "index.html").write_text("<html>conversation app</html>")
    app = create_app(Settings(frontend_dist=tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/conversations/conv_c35053a9c86649eba8f302527fd33574")

    assert response.status_code == 200
    assert response.text == "<html>conversation app</html>"
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_browser_telemetry_accepts_only_sanitized_metrics(tmp_path, monkeypatch) -> None:
    latency = Mock()
    auth = Mock()
    monkeypatch.setattr("voice_ai.voice.app.record_browser_latency", latency)
    monkeypatch.setattr("voice_ai.voice.app.record_auth_event", auth)
    app = create_app(Settings(frontend_dist=tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        duration_response = await client.post(
            "/api/telemetry",
            json={"event": "navigation_interactive", "duration_ms": 123.4},
        )
        auth_response = await client.post(
            "/api/telemetry",
            json={"event": "refresh_failed"},
        )
        ignored_response = await client.post(
            "/api/telemetry",
            json={"event": "user-controlled-value", "duration_ms": 10},
        )

    assert duration_response.status_code == 204
    assert auth_response.status_code == 204
    assert ignored_response.status_code == 204
    latency.assert_called_once_with(duration_ms=123.4, event="navigation_interactive")
    auth.assert_called_once_with(outcome="refresh_failed", stage="browser")


@pytest.mark.asyncio
async def test_voice_initialization_retries_transient_dependency_failure(
    tmp_path, monkeypatch
) -> None:
    checks = AsyncMock(
        side_effect=[
            [CheckResult("Agent service", "fail", "Agent is starting")],
            [],
        ]
    )
    monkeypatch.setattr("voice_ai.voice.runtime.run_voice_checks", checks)
    monkeypatch.setattr("voice_ai.voice.app.asyncio.sleep", AsyncMock())
    app = create_app(Settings(frontend_dist=tmp_path))

    async with app.router.lifespan_context(app):
        await app.state.voice_initialization
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/readyz")

    assert checks.await_count == 2
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.asyncio
async def test_authenticated_voice_offer_binds_to_owned_conversation(tmp_path, monkeypatch) -> None:
    class FakeRequestHandler:
        def __init__(self, **_kwargs) -> None:
            pass

        async def handle_web_request(self, **_kwargs):
            return {"pc_id": "pc_test", "sdp": "answer", "type": "answer"}

        async def close(self) -> None:
            pass

    validate = AsyncMock()
    monkeypatch.setattr(
        "voice_ai.voice.runtime.SmallWebRTCRequestHandler",
        FakeRequestHandler,
    )
    monkeypatch.setattr(
        "voice_ai.voice.runtime.run_voice_checks",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr("voice_ai.voice.app._validate_voice_conversation", validate)
    app = create_app(
        Settings(
            api_enabled=True,
            auth0_domain="tenant.example.auth0.com",
            auth0_audience="https://agent.example.com",
            auth0_spa_client_id="spa-client-id",
            frontend_dist=tmp_path,
        )
    )
    async with app.router.lifespan_context(app):
        await app.state.voice_initialization
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/offer",
                headers={
                    "Authorization": "Bearer user-token",
                    "X-Conversation-Id": "conv_test",
                    "X-Model-Id": "test:assistant",
                },
                json={"sdp": "offer", "type": "offer"},
            )

    assert response.status_code == 200
    validate.assert_awaited_once()
    assert validate.await_args.kwargs["access_token"] == "user-token"
    assert validate.await_args.kwargs["conversation_id"] == "conv_test"


@pytest.mark.asyncio
async def test_authenticated_voice_offer_requires_model_selection(tmp_path, monkeypatch) -> None:
    class FakeRequestHandler:
        def __init__(self, **_kwargs) -> None:
            pass

        async def handle_web_request(self, **_kwargs):
            return {"pc_id": "pc_test", "sdp": "answer", "type": "answer"}

        async def close(self) -> None:
            pass

    monkeypatch.setattr(
        "voice_ai.voice.runtime.SmallWebRTCRequestHandler",
        FakeRequestHandler,
    )
    monkeypatch.setattr(
        "voice_ai.voice.runtime.run_voice_checks",
        AsyncMock(return_value=[]),
    )
    app = create_app(
        Settings(
            api_enabled=True,
            auth0_domain="tenant.example.auth0.com",
            auth0_audience="https://agent.example.com",
            auth0_spa_client_id="spa-client-id",
            frontend_dist=tmp_path,
        )
    )
    async with app.router.lifespan_context(app):
        await app.state.voice_initialization
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/offer",
                headers={
                    "Authorization": "Bearer user-token",
                    "X-Conversation-Id": "conv_test",
                },
                json={"sdp": "offer", "type": "offer"},
            )

    assert response.status_code == 422
    assert response.json()["detail"] == "A model selection is required for voice"


@pytest.mark.asyncio
async def test_authenticated_voice_signaling_is_bound_to_the_creating_token(
    tmp_path, monkeypatch
) -> None:
    stop_session = asyncio.Event()
    session_finished = asyncio.Event()

    class FakeConnection:
        pc_id = "pc_owned"

    class FakeRequestHandler:
        def __init__(self, **_kwargs) -> None:
            self.connection = FakeConnection()
            self.patch_count = 0

        async def handle_web_request(self, *, request, webrtc_connection_callback):
            if request.pc_id is None:
                await webrtc_connection_callback(self.connection)
            return {"pc_id": self.connection.pc_id, "sdp": "answer", "type": "answer"}

        async def handle_patch_request(self, _request) -> None:
            self.patch_count += 1

        async def close(self) -> None:
            pass

    async def run_session_until_stopped(*, capacity, **_kwargs) -> None:
        try:
            await stop_session.wait()
        finally:
            await capacity.release()
            session_finished.set()

    monkeypatch.setattr(
        "voice_ai.voice.runtime.SmallWebRTCRequestHandler",
        FakeRequestHandler,
    )
    monkeypatch.setattr(
        "voice_ai.voice.runtime.run_voice_checks",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "voice_ai.voice.runtime.run_session",
        run_session_until_stopped,
    )
    monkeypatch.setattr(
        "voice_ai.voice.app._validate_voice_conversation",
        AsyncMock(),
    )
    app = create_app(
        Settings(
            api_enabled=True,
            auth0_domain="tenant.example.auth0.com",
            auth0_audience="https://agent.example.com",
            auth0_spa_client_id="spa-client-id",
            frontend_dist=tmp_path,
        )
    )
    owner_headers = {
        "Authorization": "Bearer owner-token",
        "X-Conversation-Id": "conv_test",
        "X-Model-Id": "test:assistant",
    }

    async with app.router.lifespan_context(app):
        await app.state.voice_initialization
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            initial = await client.post(
                "/api/offer",
                headers=owner_headers,
                json={"sdp": "offer", "type": "offer"},
            )
            same_owner = await client.post(
                "/api/offer",
                headers={"Authorization": "Bearer owner-token"},
                json={"pc_id": "pc_owned", "sdp": "offer", "type": "offer"},
            )
            wrong_owner = await client.post(
                "/api/offer",
                headers={"Authorization": "Bearer another-token"},
                json={"pc_id": "pc_owned", "sdp": "offer", "type": "offer"},
            )
            missing_token = await client.post(
                "/api/offer",
                json={"pc_id": "pc_owned", "sdp": "offer", "type": "offer"},
            )
            owner_patch = await client.patch(
                "/api/offer",
                headers={"Authorization": "Bearer owner-token"},
                json={"pc_id": "pc_owned", "candidates": []},
            )
            wrong_owner_patch = await client.patch(
                "/api/offer",
                headers={"Authorization": "Bearer another-token"},
                json={"pc_id": "pc_owned", "candidates": []},
            )
            active_health = await client.get("/healthz")

            stop_session.set()
            await asyncio.wait_for(session_finished.wait(), timeout=1)
            for _ in range(10):
                finished_health = await client.get("/healthz")
                if finished_health.json()["authenticated_sessions"] == 0:
                    break
                await asyncio.sleep(0)

    assert initial.status_code == 200
    assert same_owner.status_code == 200
    assert wrong_owner.status_code == 404
    assert missing_token.status_code == 401
    assert owner_patch.status_code == 200
    assert wrong_owner_patch.status_code == 404
    assert active_health.json()["authenticated_sessions"] == 1
    assert finished_health.json()["authenticated_sessions"] == 0
