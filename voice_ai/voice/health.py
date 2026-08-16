from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import httpx
import nltk
from faster_whisper.utils import download_model

from voice_ai.agent.models import ModelReadiness, check_configured_model
from voice_ai.agent.persistence.database import Database
from voice_ai.shared.config import VoiceSettings
from voice_ai.voice.speech.assets import kokoro_assets_valid

Status = Literal["pass", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    fix_command: str | None = None
    latency_ms: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


async def run_checks(
    settings: VoiceSettings,
    database: Database,
    *,
    include_frontend: bool = True,
) -> tuple[list[CheckResult], ModelReadiness]:
    model_task = asyncio.create_task(check_configured_model(settings))
    database_task = asyncio.create_task(_database_check(settings, database))
    whisper_task = asyncio.create_task(asyncio.to_thread(_whisper_check, settings))
    checks = [
        await database_task,
        await whisper_task,
        _nltk_check(settings),
        _tts_check(settings),
        _deployment_check(settings),
    ]
    model_status = await model_task
    checks.insert(0, _model_check(model_status))
    if include_frontend:
        checks.append(_frontend_check(settings.frontend_dist))
    return checks, model_status


async def run_voice_checks(
    settings: VoiceSettings,
    *,
    include_frontend: bool = True,
) -> list[CheckResult]:
    """Check only dependencies owned by the public voice gateway."""
    agent_task = asyncio.create_task(_agent_service_check(settings))
    whisper_task = asyncio.create_task(asyncio.to_thread(_whisper_check, settings))
    checks = [
        await agent_task,
        await whisper_task,
        _nltk_check(settings),
        _tts_check(settings),
        _deployment_check(settings),
    ]
    if include_frontend:
        checks.append(_frontend_check(settings.frontend_dist))
    return checks


def overall_status(checks: list[CheckResult]) -> str:
    if any(check.status == "fail" for check in checks):
        return "not_ready"
    if any(check.status == "warn" for check in checks):
        return "degraded"
    return "ready"


async def _agent_service_check(settings: VoiceSettings) -> CheckResult:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            response = await client.get(f"{settings.agent_base_url.rstrip('/')}/ready")
        payload = response.json()
        if response.status_code != 200:
            return CheckResult(
                "Agent service",
                "fail",
                f"Agent is not ready at {settings.agent_base_url}: {payload.get('status', 'unknown')}",
                "uv run voice-ai agent",
            )
        return CheckResult(
            "Agent service",
            "pass",
            f"Private agent API ready at {settings.agent_base_url}",
        )
    except (httpx.HTTPError, ValueError) as exc:
        return CheckResult(
            "Agent service",
            "fail",
            f"Agent is not reachable at {settings.agent_base_url}: {type(exc).__name__}",
            "uv run voice-ai agent",
        )


def _model_check(status: ModelReadiness) -> CheckResult:
    fix_command = None
    if status.provider == "ollama" and not status.ready:
        fix_command = "ollama serve"
    return CheckResult(
        "Agent model",
        "pass" if status.ready else "fail",
        f"{status.model}: {status.detail}",
        fix_command,
    )


async def _database_check(settings: VoiceSettings, database: Database) -> CheckResult:
    try:
        latency = await database.ping_ms()
        status: Status = "warn" if latency > 20 else "pass"
        detail = f"Database ready; query latency {latency:.1f} ms"
        if latency > 20:
            detail += " (above the 20 ms local budget)"
        return CheckResult("PostgreSQL", status, detail, latency_ms=latency)
    except Exception as exc:
        return CheckResult(
            "PostgreSQL",
            "fail",
            f"Database is not ready: {type(exc).__name__}: {exc}",
            "docker compose up -d postgres",
        )


def _whisper_check(settings: VoiceSettings) -> CheckResult:
    try:
        path = download_model(
            settings.whisper_model,
            local_files_only=True,
            cache_dir=str(settings.whisper_cache_dir),
            revision=settings.whisper_revision,
        )
        return CheckResult("Whisper", "pass", f"{settings.whisper_model} is cached at {path}")
    except Exception:
        return CheckResult(
            "Whisper",
            "fail",
            f"{settings.whisper_model} is not in the pinned local model cache",
            "uv run voice-ai doctor --fix --yes",
        )


def _tts_check(settings: VoiceSettings) -> CheckResult:
    if kokoro_assets_valid(settings.kokoro_download_dir):
        return CheckResult("Kokoro", "pass", f"{settings.kokoro_voice} is cached")
    return CheckResult(
        "Kokoro",
        "fail",
        f"{settings.kokoro_voice} is missing or failed checksum validation",
        "uv run voice-ai doctor --fix --yes",
    )


def _nltk_check(settings: VoiceSettings) -> CheckResult:
    nltk_dir = settings.model_cache_dir / "nltk"
    try:
        nltk.data.find("tokenizers/punkt_tab", paths=[str(nltk_dir)])
        return CheckResult("Sentence tokenizer", "pass", f"NLTK punkt_tab is cached at {nltk_dir}")
    except LookupError:
        return CheckResult(
            "Sentence tokenizer",
            "fail",
            "NLTK punkt_tab is absent; Pipecat sentence aggregation may fail offline",
            "uv run voice-ai doctor --fix --yes",
        )


def _deployment_check(settings: VoiceSettings) -> CheckResult:
    errors = settings.public_profile_errors()
    if errors:
        return CheckResult(
            "HTTPS / ICE",
            "fail",
            "; ".join(errors),
            "Edit PUBLIC_BASE_URL and ICE_SERVERS in .env",
        )
    if settings.deployment_profile == "laptop":
        return CheckResult(
            "HTTPS / ICE",
            "warn",
            "Laptop profile: phones require trusted HTTPS and a LAN without client isolation",
        )
    return CheckResult("HTTPS / ICE", "pass", "Public HTTPS profile includes a TURN relay")


def _frontend_check(path: Path) -> CheckResult:
    index = path / "index.html"
    if index.is_file():
        return CheckResult("Browser bundle", "pass", f"Static bundle ready at {path}")
    return CheckResult(
        "Browser bundle",
        "fail",
        f"{index} is missing",
        "npm --prefix frontend ci && npm --prefix frontend run build",
    )
