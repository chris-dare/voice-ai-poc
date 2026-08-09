#!/usr/bin/env python3
"""Validate semantic invariants in a rendered Docker Compose deployment."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


def _urls(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _validate_build_context(errors: list[str]) -> None:
    ignored = {
        line.strip()
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        ".git",
        ".env",
        ".env.*",
        ".venv",
        ".run",
        "*.key",
        "*.pem",
        "frontend/node_modules",
        "models",
    }
    missing = sorted(required - ignored)
    if missing:
        errors.append(f".dockerignore does not exclude: {', '.join(missing)}")


def _validate_local(services: dict[str, Any], *, local_model: bool) -> list[str]:
    errors: list[str] = []
    required = {"postgres", "migrate", "agent", "agent-worker", "voice-gateway"}
    missing = sorted(required - services.keys())
    if missing:
        errors.append(f"local graph is missing services: {', '.join(missing)}")
    ollama = {"ollama", "ollama-init"}
    present = ollama & services.keys()
    if local_model and present != ollama:
        errors.append("local-model graph must contain both ollama and ollama-init")
    if not local_model and present:
        errors.append("remote-model graph must not start Ollama")
    _validate_build_context(errors)
    return errors


def _validate_production(services: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    expected = {"migrate", "agent", "agent-worker", "voice-gateway"}
    if set(services) != expected:
        errors.append("production services must be exactly: " + ", ".join(sorted(expected)))

    for name, service in services.items():
        if service.get("ports"):
            errors.append(f"{name} must not publish a host port")
        if service.get("read_only") is not True:
            errors.append(f"{name} must use a read-only root filesystem")
        if "ALL" not in service.get("cap_drop", []):
            errors.append(f"{name} must drop all Linux capabilities")
        if "no-new-privileges:true" not in service.get("security_opt", []):
            errors.append(f"{name} must disable privilege escalation")
        if int(service.get("pids_limit") or 0) <= 0:
            errors.append(f"{name} must have a positive process limit")
        logging_options = service.get("logging", {}).get("options", {})
        if not logging_options.get("max-size") or not logging_options.get("max-file"):
            errors.append(f"{name} must bound local container logs")
        image = str(service.get("image") or "")
        if not re.search(r"@sha256:[0-9a-f]{64}$", image):
            errors.append(f"{name} image must be pinned by sha256 digest")

    for name in ("agent", "voice-gateway"):
        test = services.get(name, {}).get("healthcheck", {}).get("test", [])
        rendered = " ".join(str(item) for item in test)
        if "/livez" not in rendered or "/readyz" in rendered:
            errors.append(f"{name} container healthcheck must use /livez")

    agent_env = services.get("agent", {}).get("environment", {})
    if agent_env.get("AGENT_DEPLOYMENT_PROFILE") != "public":
        errors.append("agent must use the public deployment profile")
    if agent_env.get("AGENT_EMBEDDED_WORKER") != "false":
        errors.append("agent must not embed a response worker")
    if agent_env.get("API_ENABLED") != "true":
        errors.append("agent public API must be enabled")
    if not str(agent_env.get("DATABASE_URL") or "").startswith("postgresql+asyncpg://"):
        errors.append("agent must use PostgreSQL through asyncpg")
    if len(str(agent_env.get("AGENT_SHARED_SECRET") or "")) < 32:
        errors.append("agent internal credential must contain at least 32 characters")
    if agent_env.get("AGENT_HOST") != "0.0.0.0":
        errors.append("agent must bind inside its container network")
    if int(agent_env.get("API_MAX_REQUEST_BODY_BYTES") or 0) <= 0:
        errors.append("agent must have a positive HTTP request-body limit")

    voice_env = services.get("voice-gateway", {}).get("environment", {})
    if voice_env.get("DEPLOYMENT_PROFILE") != "public":
        errors.append("voice gateway must use the public deployment profile")
    if not str(voice_env.get("PUBLIC_BASE_URL") or "").startswith("https://"):
        errors.append("voice gateway public base URL must use HTTPS")
    if voice_env.get("AGENT_BASE_URL") != "http://agent:8100":
        errors.append("voice gateway must use the private agent service address")
    try:
        ice_servers = json.loads(str(voice_env.get("ICE_SERVERS") or "[]"))
    except json.JSONDecodeError:
        ice_servers = []
    has_turn = any(
        any(url.startswith(("turn:", "turns:")) for url in _urls(server.get("urls")))
        for server in ice_servers
        if isinstance(server, dict)
    )
    if not has_turn:
        errors.append("voice gateway ICE_SERVERS must contain a TURN URL")
    if int(voice_env.get("MAX_CONCURRENT_SESSIONS") or 0) <= 0:
        errors.append("voice gateway must have a positive session limit")
    if int(voice_env.get("VOICE_MAX_REQUEST_BODY_BYTES") or 0) <= 0:
        errors.append("voice gateway must have a positive HTTP request-body limit")

    migrate_env = services.get("migrate", {}).get("environment", {})
    if set(migrate_env) != {"DATABASE_URL"}:
        errors.append("migration job must receive only the database credential")
    _validate_build_context(errors)
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "profile",
        choices=("remote-local", "local-model", "production"),
    )
    args = parser.parse_args()
    document = json.load(sys.stdin)
    services = document.get("services")
    if not isinstance(services, dict):
        print("Rendered Compose document has no services object.", file=sys.stderr)
        return 1
    if args.profile == "production":
        errors = _validate_production(services)
    else:
        errors = _validate_local(services, local_model=args.profile == "local-model")
    if errors:
        for error in errors:
            print(f"deployment validation: {error}", file=sys.stderr)
        return 1
    print(f"deployment validation passed: {args.profile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
