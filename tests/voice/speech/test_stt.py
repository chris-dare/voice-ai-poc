from __future__ import annotations

from voice_ai.voice.speech import stt


def test_whisper_uses_the_pinned_service_cache(tmp_path, monkeypatch) -> None:
    observed = {}

    class FakeWhisperModel:
        def __init__(self, model, **kwargs) -> None:
            observed["model"] = model
            observed.update(kwargs)

    monkeypatch.setattr(stt, "WhisperModel", FakeWhisperModel)

    stt.LocalWhisperSTTService(
        model="base",
        revision="a" * 40,
        cache_dir=tmp_path,
    )

    assert observed == {
        "model": "base",
        "device": "cpu",
        "compute_type": "int8",
        "download_root": str(tmp_path),
        "revision": "a" * 40,
    }
