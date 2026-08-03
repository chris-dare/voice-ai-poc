from __future__ import annotations

import asyncio
import platform
import shutil
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi import RTVIProcessor
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.piper.tts import PiperTTSService
from pipecat.services.tts_service import TTSService
from pipecat.transports.base_transport import TransportParams
from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from voice_ai.shared.config import Settings
from voice_ai.shared.observability import create_latency_observer
from voice_ai.voice.agent_client import RemoteAgentLLMService
from voice_ai.voice.speech.filters import SpeechSafetyFilter, TranscriptGuard
from voice_ai.voice.speech.stt import LocalWhisperSTTService
from voice_ai.voice.speech.tts import MacSayTTSService


@dataclass(slots=True)
class SessionResources:
    session_id: UUID
    rtvi: RTVIProcessor


@dataclass(slots=True)
class VoiceSession:
    worker: PipelineWorker
    transport: SmallWebRTCTransport
    rtvi: RTVIProcessor
    tts: TTSService
    degraded_tts: bool


def create_voice_session(
    *,
    connection: SmallWebRTCConnection,
    settings: Settings,
    public_access_token: str | None = None,
    conversation_id: str | None = None,
) -> VoiceSession:
    """Build audio I/O only; reasoning and tools live behind the agent API."""
    transport = SmallWebRTCTransport(
        webrtc_connection=connection,
        params=TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16_000,
            audio_out_sample_rate=24_000,
        ),
    )
    rtvi = RTVIProcessor()
    resources = SessionResources(session_id=uuid4(), rtvi=rtvi)

    stt = LocalWhisperSTTService(
        model=settings.whisper_model,
        language="en",
        no_speech_prob=0.4,
        device="cpu",
        compute_type="int8",
    )
    llm = RemoteAgentLLMService(
        base_url=settings.agent_base_url,
        session_id=resources.session_id,
        shared_secret=settings.agent_shared_secret,
        on_event=lambda message: _send(rtvi, message),
        public_access_token=public_access_token,
        conversation_id=conversation_id,
    )
    tts, degraded_tts = _create_tts(settings)
    context_aggregator = LLMContextAggregatorPair(
        LLMContext(),
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_turn_strategies=UserTurnStrategies(
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.5)]
            ),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            TranscriptGuard(),
            context_aggregator.user(),
            llm,
            SpeechSafetyFilter(),
            tts,
            transport.output(),
            context_aggregator.assistant(),
        ]
    )
    latency_observer = create_latency_observer(lambda message: _send(rtvi, message))
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=16_000,
            audio_out_sample_rate=24_000,
            enable_metrics=True,
            enable_usage_metrics=True,
            send_initial_empty_metrics=False,
        ),
        app_resources=resources,
        enable_rtvi=True,
        rtvi_processor=rtvi,
        observers=[latency_observer],
        enable_tracing=bool(settings.otlp_endpoint or settings.logfire_enabled),
        idle_timeout_secs=300,
        cancel_runner_on_idle_timeout=False,
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport, _client) -> None:
        await worker.cancel(reason="WebRTC client disconnected")

    return VoiceSession(
        worker=worker,
        transport=transport,
        rtvi=rtvi,
        tts=tts,
        degraded_tts=degraded_tts,
    )


async def warm_tts_service(tts: TTSService) -> float:
    """Run and discard one short synthesis so the first reply uses a hot model."""
    started = perf_counter()
    if isinstance(tts, KokoroTTSService):
        voice = tts._settings.voice
        language = tts._settings.language
        if not voice or not language:
            raise RuntimeError("Kokoro warm-up requires a configured voice and language")
        audio_samples = 0
        async for samples, _sample_rate in tts._kokoro.create_stream(
            "Ready.",
            voice=voice,
            lang=language,
            speed=1.0,
        ):
            audio_samples += len(samples)
        if audio_samples == 0:
            raise RuntimeError("Kokoro warm-up produced no audio")
    elif isinstance(tts, PiperTTSService):

        def synthesize() -> int:
            return sum(len(chunk.audio_int16_bytes) for chunk in tts._voice.synthesize("Ready."))

        if await asyncio.to_thread(synthesize) == 0:
            raise RuntimeError("Piper warm-up produced no audio")
    else:
        logger.debug("TTS warm-up is not available for {}", type(tts).__name__)
        return 0.0

    elapsed = perf_counter() - started
    logger.info("{} warm-up completed in {:.2f}s", type(tts).__name__, elapsed)
    return elapsed


def _create_tts(settings: Settings) -> tuple[TTSService, bool]:
    if settings.tts_provider == "kokoro":
        kokoro_model = settings.kokoro_download_dir / "kokoro-v1.0.onnx"
        kokoro_voices = settings.kokoro_download_dir / "voices-v1.0.bin"
        if kokoro_model.is_file() and kokoro_voices.is_file():
            return (
                KokoroTTSService(
                    model_path=str(kokoro_model),
                    voices_path=str(kokoro_voices),
                    settings=KokoroTTSService.Settings(voice=settings.kokoro_voice),
                ),
                False,
            )
        logger.warning("Kokoro is unavailable; falling back to Piper")

    model_path = settings.piper_download_dir / f"{settings.piper_voice}.onnx"
    config_path = settings.piper_download_dir / f"{settings.piper_voice}.onnx.json"
    if model_path.exists() and config_path.exists():
        return (
            PiperTTSService(
                download_dir=settings.piper_download_dir,
                settings=PiperTTSService.Settings(voice=settings.piper_voice),
            ),
            False,
        )
    if platform.system() == "Darwin" and shutil.which("say"):
        logger.warning("Piper is unavailable; using the degraded macOS say fallback")
        return MacSayTTSService(), True
    raise RuntimeError("Piper voice files are missing; run `voice-ai doctor --fix --yes`")


async def _send(rtvi: RTVIProcessor, message: dict[str, Any]) -> None:
    try:
        await rtvi.send_server_message(message)
    except Exception:
        logger.debug("RTVI message could not be delivered", exc_info=True)
