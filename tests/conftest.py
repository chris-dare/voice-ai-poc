from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def configured_test_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests independent of developer machines and real provider configuration."""
    monkeypatch.setenv("AGENT_MODEL", "test:assistant")
    monkeypatch.setenv("AGENT_MODELS", "[]")
