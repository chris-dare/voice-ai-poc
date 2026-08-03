from types import SimpleNamespace

import pytest

from voice_ai.voice import pipeline


class _FakeKokoroEngine:
    def __init__(self) -> None:
        self.language: str | None = None

    async def create_stream(self, _text: str, *, voice: str, lang: str, speed: float):
        assert voice == "af_heart"
        assert speed == 1.0
        self.language = lang
        yield [0, 1, 2], 24_000


@pytest.mark.asyncio
async def test_kokoro_warmup_uses_service_language_and_discards_audio(monkeypatch) -> None:
    class FakeKokoroTTSService:
        def __init__(self) -> None:
            self._settings = SimpleNamespace(voice="af_heart", language="en-us")
            self._kokoro = _FakeKokoroEngine()

    monkeypatch.setattr(pipeline, "KokoroTTSService", FakeKokoroTTSService)
    service = FakeKokoroTTSService()

    elapsed = await pipeline.warm_tts_service(service)

    assert elapsed >= 0
    assert service._kokoro.language == "en-us"
