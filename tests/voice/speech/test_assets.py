from __future__ import annotations

import hashlib
from io import BytesIO

import pytest

from voice_ai.voice.speech import assets


def test_verified_download_repairs_an_invalid_cached_asset(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "asset.bin"
    destination.write_bytes(b"corrupt")
    expected = b"verified model bytes"
    monkeypatch.setattr(assets, "urlopen", lambda *_args, **_kwargs: BytesIO(expected))

    assets._download_verified(
        "https://models.example/asset.bin",
        destination,
        hashlib.sha256(expected).hexdigest(),
    )

    assert destination.read_bytes() == expected
    assert not destination.with_suffix(".bin.part").exists()


def test_failed_checksum_never_replaces_existing_asset(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "asset.bin"
    destination.write_bytes(b"existing")
    monkeypatch.setattr(assets, "urlopen", lambda *_args, **_kwargs: BytesIO(b"tampered"))

    with pytest.raises(RuntimeError, match="checksum did not match"):
        assets._download_verified(
            "https://models.example/asset.bin",
            destination,
            hashlib.sha256(b"expected").hexdigest(),
        )

    assert destination.read_bytes() == b"existing"
    assert not destination.with_suffix(".bin.part").exists()


def test_valid_cached_asset_does_not_make_a_network_request(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "asset.bin"
    content = b"already verified"
    destination.write_bytes(content)

    def unexpected_download(*_args, **_kwargs):
        raise AssertionError("a valid cached asset must not be downloaded again")

    monkeypatch.setattr(assets, "urlopen", unexpected_download)

    assets._download_verified(
        "https://models.example/asset.bin",
        destination,
        hashlib.sha256(content).hexdigest(),
    )
