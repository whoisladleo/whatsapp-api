import logging

import pytest

from app.core.config import Settings
from app.services.llm import (
    ClaudeProvider,
    LLMService,
    MuseProvider,
)


def test_llm_provider_setting_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert Settings().LLM_PROVIDER == "claude"
    assert Settings.model_fields["LLM_PROVIDER"].default == "claude"


def test_service_defaults_to_claude_provider(monkeypatch):
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")
    service = LLMService(api_key=None)

    assert isinstance(service.provider, ClaudeProvider)
    assert service.client is None
    assert service.model_id == "claude-sonnet-5"


def test_claude_provider_constructs_with_key():
    service = LLMService(api_key="test-anthropic-key", provider="claude")

    assert isinstance(service.provider, ClaudeProvider)
    assert service.client is not None
    assert service.model_id == "claude-sonnet-5"


def test_muse_provider_constructs_with_key():
    service = LLMService(api_key="test-model-key", provider="muse")

    assert isinstance(service.provider, MuseProvider)
    assert service.client is not None
    assert service.model_id == "muse-spark-1.3"
    assert "api.meta.ai" in str(service.client.base_url)


def test_muse_provider_without_key_has_no_client():
    service = LLMService(api_key=None, provider="muse")

    assert isinstance(service.provider, MuseProvider)
    assert service.client is None


def test_unknown_provider_warns_and_falls_back_to_claude(caplog):
    with caplog.at_level(logging.WARNING, logger="app.services.llm"):
        service = LLMService(api_key=None, provider="msue")

    assert isinstance(service.provider, ClaudeProvider)
    assert service.model_id == "claude-sonnet-5"
    assert any(
        r.name == "app.services.llm" and r.levelno >= logging.WARNING
        for r in caplog.records
    )


@pytest.mark.parametrize("provider", ["claude", "muse"])
def test_known_providers_do_not_warn(caplog, provider):
    with caplog.at_level(logging.WARNING, logger="app.services.llm"):
        LLMService(api_key=None, provider=provider)

    assert not [
        r for r in caplog.records
        if r.name == "app.services.llm" and r.levelno >= logging.WARNING
    ]
