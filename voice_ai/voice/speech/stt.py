from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel
from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601
from pipecat.utils.tracing.service_decorators import traced_stt


class LocalWhisperSTTService(SegmentedSTTService):
    """Pipecat segmented STT backed directly by Faster Whisper on CPU.

    Pipecat 1.5 imports MLX eagerly on Apple Silicon even for its CPU service. This local
    adapter retains Pipecat's segmentation, frames, metrics and interruption lifecycle while
    avoiding a Metal dependency, which is required for headless and CPU-only demonstrations.
    """

    def __init__(
        self,
        *,
        model: str = "base",
        revision: str | None = None,
        cache_dir: Path | None = None,
        language: str = "en",
        no_speech_prob: float = 0.4,
        device: str = "cpu",
        compute_type: str = "int8",
        **kwargs,
    ) -> None:
        super().__init__(
            settings=STTSettings(model=model, language=language, extra={}),
            **kwargs,
        )
        self._no_speech_prob = no_speech_prob
        logger.info("Loading Faster Whisper model {} on {}", model, device)
        self._model = WhisperModel(
            model,
            device=device,
            compute_type=compute_type,
            download_root=str(cache_dir) if cache_dir is not None else None,
            revision=revision,
        )

    @property
    def wants_wav_segments(self) -> bool:
        return False

    def can_generate_metrics(self) -> bool:
        return True

    @traced_stt
    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if not audio:
            return
        await self.start_processing_metrics()
        try:
            samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0
            language = str(self._settings.language or "en")
            segments, _ = await asyncio.to_thread(
                self._model.transcribe,
                samples,
                language=language,
            )
            text = " ".join(
                segment.text.strip()
                for segment in segments
                if segment.text.strip() and segment.no_speech_prob < self._no_speech_prob
            ).strip()
            if text:
                yield TranscriptionFrame(
                    text,
                    self._user_id,
                    time_now_iso8601(),
                    language,
                )
        except Exception as exc:
            logger.exception("Local Whisper transcription failed")
            yield ErrorFrame(error=f"Local Whisper failed: {exc}")
        finally:
            await self.stop_processing_metrics()
