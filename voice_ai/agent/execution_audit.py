from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic_ai.models import parse_model_id

from voice_ai.agent.models import ModelSelection
from voice_ai.shared.config import AgentSettings

EXECUTION_AUDIT_SCHEMA_VERSION = "1"
_POLICY_CONTRACT = "core-execution-policy-v1"


def enabled_capabilities(settings: AgentSettings) -> list[str]:
    """Return the effective, ordered capability set presented by this agent definition."""
    capabilities = ["general_assistance", "web_research", "code_execution"]
    if settings.agent_deep_agents_enabled:
        capabilities.append("specialist_delegation")
    if settings.agent_planning_enabled:
        capabilities.append("planning")
    if settings.mcp_config_path is not None:
        capabilities.append("remote_tools")
    return capabilities


def execution_limits(settings: AgentSettings) -> dict[str, int | float]:
    """Return the server-enforced response limits published to operators and audits."""
    return {
        "max_execution_seconds": settings.agent_execution_timeout_seconds,
        "max_required_action_pause_seconds": settings.confirmation_ttl_seconds,
        "max_model_requests": settings.agent_request_limit,
        "max_total_tokens": settings.agent_total_token_limit,
        "max_tool_calls": settings.agent_tool_call_limit,
        "max_output_bytes": settings.agent_max_output_bytes,
        "max_delegation_depth": 1 if settings.agent_deep_agents_enabled else 0,
        "max_specialist_fanout": 3 if settings.agent_deep_agents_enabled else 0,
        "max_concurrent_work": settings.agent_worker_concurrency,
        "max_concurrent_model_requests_per_route": settings.agent_model_route_concurrency,
        "max_concurrent_tool_calls_per_route": settings.agent_tool_route_concurrency,
        "max_queued_work": settings.agent_queue_capacity,
        "max_active_responses_per_tenant": settings.agent_tenant_active_response_limit,
        "max_stream_buffer_events": settings.agent_stream_buffer_capacity,
        "max_subagent_model_requests": settings.agent_subagent_request_limit,
        "max_subagent_tool_calls": settings.agent_subagent_tool_call_limit,
        "max_subagent_total_tokens": settings.agent_subagent_total_token_limit,
    }


def build_execution_snapshot(
    settings: AgentSettings,
    *,
    agent_id: str,
    model_id: str,
    definition_source: str,
    selection: ModelSelection | None,
) -> dict[str, Any]:
    """Build a credential-free, immutable explanation of the accepted execution policy."""
    capabilities = enabled_capabilities(settings)
    limits = execution_limits(settings)
    provider, _ = parse_model_id(model_id)
    route = {
        "selected_model_id": model_id,
        "canonical_model_id": selection.primary_id if selection else model_id,
        "provider": selection.provider if selection else (provider or "unknown"),
        "gateway": selection.gateway if selection else provider == "openrouter",
        "fallback_model_ids": list(selection.fallback_ids) if selection else [],
        "settings": {
            "thinking_effort": settings.agent_thinking_effort,
            "max_output_tokens": settings.agent_max_output_tokens,
        },
    }
    definition_material = {
        "release_version": settings.agent_release_version,
        "agent_id": agent_id,
        "definition_source_sha256": _sha256(definition_source),
        "capabilities": capabilities,
        "deep_thinking_effort": settings.agent_deep_thinking_effort,
        "subagent_model_id": settings.agent_subagent_model,
    }
    limit_policy = {
        "version": _version({"limits": limits}),
        "limits": limits,
    }
    return {
        "schema_version": EXECUTION_AUDIT_SCHEMA_VERSION,
        "agent_definition": {
            "id": agent_id,
            "version": _version(definition_material),
            "release_version": settings.agent_release_version,
        },
        "capability_set": capabilities,
        "model_route": route,
        "limit_policy": limit_policy,
        "policy_version": _version(
            {
                "contract": _POLICY_CONTRACT,
                "release_version": settings.agent_release_version,
            }
        ),
    }


def _version(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{_sha256(canonical)}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
