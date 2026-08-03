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
    "https://github.com/thewh1teagle/kokoro-onnx/"
    "releases/download/model-files-v1.0/voices-v1.0.bin"
)
PUNKT_TAB_URL = (
    "https://raw.githubusercontent.com/nltk/nltk_data/"
    "gh-pages/packages/tokenizers/punkt_tab.zip"
)
PUNKT_TAB_SHA256 = "e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106"


def provision_kokoro(destination: Path) -> None:
    """Download Kokoro's shared model and voice bank for offline runtime use."""
    _download_if_missing(KOKORO_MODEL_URL, destination / "kokoro-v1.0.onnx")
    _download_if_missing(KOKORO_VOICES_URL, destination / "voices-v1.0.bin")


def _download_if_missing(url: str, destination: Path) -> None:
    if destination.is_file() and destination.stat().st_size > 0:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.part")
    try:
        with urlopen(url, timeout=300) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def provision_punkt_tab(destination: Path) -> None:
    """Install the pinned NLTK tokenizer without its environment-sensitive downloader."""
    installed = destination / "tokenizers" / "punkt_tab"
    if installed.is_dir() and any(installed.iterdir()):
        return

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
