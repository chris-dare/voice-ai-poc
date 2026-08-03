from __future__ import annotations

import aifc
import asyncio
import os
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import ErrorFrame, Frame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService


@dataclass
class MacSaySettings(TTSSettings):
    pass


class MacSayTTSService(TTSService):
    """Clearly degraded, local-only TTS fallback for macOS development."""

    Settings = MacSaySettings

    def __init__(self, *, voice: str = "Samantha", **kwargs) -> None:
        settings = self.Settings(model="macos-say", voice=voice, language="en")
        super().__init__(
            settings=settings,
            push_start_frame=True,
            push_stop_frames=True,
            **kwargs,
        )

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        path: Path | None = None
        try:
            await self.start_tts_usage_metrics(text)
            with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as output:
                path = Path(output.name)
            process = await asyncio.create_subprocess_exec(
                "say",
                "-v",
                str(self._settings.voice),
                "--data-format=LEI16@24000",
                "-o",
                str(path),
                text,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode:
                raise RuntimeError(stderr.decode("utf-8", "replace").strip())

            audio, sample_rate = await asyncio.to_thread(_read_aiff, path)

            async def chunks() -> AsyncIterator[bytes]:
                chunk_size = 4_800
                for offset in range(0, len(audio), chunk_size):
                    yield audio[offset : offset + chunk_size]

            async for frame in self._stream_audio_frames_from_iterator(
                chunks(), in_sample_rate=sample_rate, context_id=context_id
            ):
                await self.stop_ttfb_metrics()
                yield frame
        except Exception as exc:
            logger.exception("macOS say fallback failed")
            yield ErrorFrame(error=f"macOS speech fallback failed: {exc}")
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
            await self.stop_ttfb_metrics()


def _read_aiff(path: Path) -> tuple[bytes, int]:
    with aifc.open(os.fspath(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise RuntimeError("macOS say returned an unsupported audio format")
        return audio.readframes(audio.getnframes()), audio.getframerate()
