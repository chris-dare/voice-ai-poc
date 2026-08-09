from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from pipecat.transports.smallwebrtc.connection import IceServer, SmallWebRTCConnection
from pipecat.transports.smallwebrtc.request_handler import (
    IceCandidate,
    SmallWebRTCPatchRequest,
    SmallWebRTCRequest,
    SmallWebRTCRequestHandler,
)
from pipecat.workers.runner import WorkerRunner

from voice_ai.shared.config import VoiceSettings
from voice_ai.voice.health import overall_status, run_voice_checks
from voice_ai.voice.pipeline import create_voice_session, warm_tts_service


class VoiceRuntime:
    """Heavy voice dependencies loaded outside the web UI startup path."""

    def __init__(self, settings: VoiceSettings) -> None:
        self.settings = settings
        ice_servers = [
            IceServer(
                urls=server.urls,
                username=server.username,
                credential=server.credential,
            )
            for server in settings.ice_servers
        ]
        self.request_handler = SmallWebRTCRequestHandler(
            ice_servers=ice_servers,
            host=settings.host,
        )

    async def checks(self):
        return await run_voice_checks(self.settings)

    @staticmethod
    def status(checks) -> str:
        return overall_status(checks)

    @staticmethod
    def parse_offer(payload: dict[str, Any]) -> SmallWebRTCRequest:
        return SmallWebRTCRequest(**payload)

    @staticmethod
    def parse_patch(payload: dict[str, Any]) -> SmallWebRTCPatchRequest:
        candidates = [
            candidate if isinstance(candidate, IceCandidate) else IceCandidate(**candidate)
            for candidate in payload.get("candidates", [])
        ]
        return SmallWebRTCPatchRequest(pc_id=payload["pc_id"], candidates=candidates)

    async def close(self) -> None:
        await self.request_handler.close()


async def run_session(
    *,
    connection: SmallWebRTCConnection,
    settings: VoiceSettings,
    capacity: Any,
    public_access_token: str | None = None,
    conversation_id: str | None = None,
    model_id: str | None = None,
) -> None:
    try:
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        session = create_voice_session(
            connection=connection,
            settings=settings,
            public_access_token=public_access_token,
            conversation_id=conversation_id,
            model_id=model_id,
        )
        await runner.add_workers(session.worker)
        runner_task = asyncio.create_task(runner.run())
        await asyncio.sleep(0)
        try:
            await warm_tts_service(session.tts)
        except Exception:
            logger.exception("TTS warm-up failed; continuing with cold synthesis")
        await session.rtvi.send_server_message(
            {
                "type": "session-status",
                "data": {
                    "state": "ready",
                    "detail": "Speech and agent services ready",
                },
            }
        )
        await runner_task
    except asyncio.CancelledError:
        if "runner" in locals():
            await runner.cancel(reason="Server shutting down")
        raise
    except Exception:
        logger.exception("Voice session failed")
        connection.send_app_message(
            {
                "type": "server-message",
                "data": {
                    "type": "session-error",
                    "data": {"message": "The voice session could not start"},
                },
            }
        )
        await connection.disconnect()
    finally:
        await capacity.release()
