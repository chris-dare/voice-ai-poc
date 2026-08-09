from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.request import urlopen
from zipfile import ZipFile

KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/"
    "releases/download/model-files-v1.0/kokoro-v1.0.onnx"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
)
KOKORO_MODEL_SHA256 = "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
KOKORO_VOICES_SHA256 = "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
PUNKT_TAB_URL = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip"
)
PUNKT_TAB_SHA256 = "e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106"


def provision_kokoro(destination: Path) -> None:
    """Download Kokoro's shared model and voice bank for offline runtime use."""
    _download_verified(
        KOKORO_MODEL_URL,
        destination / "kokoro-v1.0.onnx",
        KOKORO_MODEL_SHA256,
    )
    _download_verified(
        KOKORO_VOICES_URL,
        destination / "voices-v1.0.bin",
        KOKORO_VOICES_SHA256,
    )


def kokoro_assets_valid(destination: Path) -> bool:
    """Return whether both provisioned Kokoro assets match their release digests."""
    return (
        _sha256_file(destination / "kokoro-v1.0.onnx") == KOKORO_MODEL_SHA256
        and _sha256_file(destination / "voices-v1.0.bin") == KOKORO_VOICES_SHA256
    )


def _download_verified(url: str, destination: Path, expected_sha256: str) -> None:
    if _sha256_file(destination) == expected_sha256:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.part")
    digest = hashlib.sha256()
    try:
        # `url` is never caller-supplied: the only call sites pass the pinned
        # KOKORO_* constants above, so no 'file://' scheme can reach urlopen.
        # nosemgrep
        with urlopen(url, timeout=300) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected_sha256:
            raise RuntimeError(f"The downloaded asset checksum did not match: {destination.name}")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def provision_punkt_tab(destination: Path) -> None:
    """Install the pinned NLTK tokenizer without its environment-sensitive downloader."""
    installed = destination / "tokenizers" / "punkt_tab"
    if installed.is_dir() and any(installed.iterdir()):
        return

    # A pinned constant URL, and the payload is checksummed below before use.
    # nosemgrep
    with urlopen(PUNKT_TAB_URL, timeout=60) as response:
        archive = response.read()
    if hashlib.sha256(archive).hexdigest() != PUNKT_TAB_SHA256:
        raise RuntimeError("The NLTK punkt_tab archive checksum did not match")

    tokenizers = destination / "tokenizers"
    tokenizers.mkdir(parents=True, exist_ok=True)
    with ZipFile(BytesIO(archive)) as package:
        for member in package.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError("The NLTK punkt_tab archive contains an unsafe path")
        package.extractall(tokenizers)
