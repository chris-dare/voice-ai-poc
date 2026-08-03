from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    model: str
    reachable: bool
    installed: bool
    capabilities: frozenset[str]
    detail: str = ""

    @property
    def supports_tools(self) -> bool:
        return "tools" in self.capabilities

    @property
    def supports_thinking(self) -> bool:
        return "thinking" in self.capabilities


async def probe_ollama(
    base_url: str,
    model: str,
    *,
    request_timeout: float = 3.0,
    client: httpx.AsyncClient | None = None,
) -> ModelCapabilities:
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=request_timeout)
    try:
        response = await http.post(
            f"{base_url.rstrip('/')}/api/show",
            json={"model": model},
            timeout=request_timeout,
        )
        if response.status_code == 404:
            return ModelCapabilities(model, True, False, frozenset(), "model is not pulled")
        response.raise_for_status()
        payload = response.json()
        return ModelCapabilities(
            model=model,
            reachable=True,
            installed=True,
            capabilities=frozenset(payload.get("capabilities") or ()),
        )
    except (httpx.HTTPError, ValueError) as exc:
        return ModelCapabilities(model, False, False, frozenset(), str(exc))
    finally:
        if owns_client:
            await http.aclose()
