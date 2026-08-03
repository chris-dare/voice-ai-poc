from __future__ import annotations

import json

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMTextFrame,
    LLMThoughtTextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class StatefulThinkStripper:
    """Remove streamed think/thinking blocks even when tags span frame boundaries."""

    _open_tags = ("<think>", "<thinking>")
    _close_tags = ("</think>", "</thinking>")

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        output: list[str] = []
        while self._buffer:
            lowered = self._buffer.lower()
            if self._inside:
                match = _first_tag(lowered, self._close_tags)
                if match is None:
                    self._buffer = _possible_tag_suffix(self._buffer, self._close_tags)
                    break
                position, tag = match
                self._buffer = self._buffer[position + len(tag) :]
                self._inside = False
                continue

            match = _first_tag(lowered, self._open_tags)
            if match is not None:
                position, tag = match
                output.append(self._buffer[:position])
                self._buffer = self._buffer[position + len(tag) :]
                self._inside = True
                continue

            suffix = _possible_tag_suffix(self._buffer, self._open_tags)
            emit_length = len(self._buffer) - len(suffix)
            output.append(self._buffer[:emit_length])
            self._buffer = suffix
            break
        return "".join(output)

    def finish(self) -> str:
        output = "" if self._inside else self._buffer
        self.reset()
        return output

    def reset(self) -> None:
        self._buffer = ""
        self._inside = False


class SpeechSafetyFilter(FrameProcessor):
    """A final text firewall before TTS."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._stripper = StatefulThinkStripper()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction is FrameDirection.UPSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMThoughtTextFrame):
            return
        if isinstance(frame, LLMTextFrame):
            clean = self._stripper.feed(frame.text)
            clean = clean.translate(str.maketrans("", "", "`*#"))
            if clean and not _looks_like_raw_json(clean):
                frame.text = clean
                await self.push_frame(frame, direction)
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            trailing = self._stripper.finish()
            if trailing and not _looks_like_raw_json(trailing):
                await self.push_frame(LLMTextFrame(trailing), direction)
        await self.push_frame(frame, direction)


class TranscriptGuard(FrameProcessor):
    """Drop empty and near-empty Whisper output before it reaches the LLM."""

    _noise = frozenset({"", ".", "..", "...", "uh", "um", "hmm", "[music]", "[silence]"})

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            normalized = " ".join(frame.text.lower().strip().split())
            if normalized in self._noise or len(normalized.replace(".", "")) < 2:
                return
        await self.push_frame(frame, direction)


def _first_tag(value: str, tags: tuple[str, ...]) -> tuple[int, str] | None:
    matches = [(value.find(tag), tag) for tag in tags if value.find(tag) >= 0]
    return min(matches, default=None, key=lambda item: item[0])


def _possible_tag_suffix(value: str, tags: tuple[str, ...]) -> str:
    lowered = value.lower()
    for length in range(min(len(value), max(map(len, tags)) - 1), 0, -1):
        suffix = lowered[-length:]
        if any(tag.startswith(suffix) for tag in tags):
            return value[-length:]
    return ""


def _looks_like_raw_json(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return True
