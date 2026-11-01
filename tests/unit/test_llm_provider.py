import logging

import anthropic
import httpx
import openai
import pytest

from app.core.config import Settings
from app.db.models import (
    InstagramAccount,
    InstagramConversationContext,
    InstagramCustomer,
    Product,
    User,
    WhatsAppAccount,
)
from app.services.llm import (
    _is_transient_llm_error,
    ClaudeProvider,
    LLMService,
    LLMUnavailable,
    MuseProvider,
)


def test_llm_provider_setting_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert Settings(_env_file=None).LLM_PROVIDER == "claude"
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


@pytest.mark.parametrize("provider", ["claude", "muse"])
def test_providers_disable_http_keepalive(monkeypatch, provider):
    # Pooled connections bind to their creating loop; workers run every
    # task in a fresh asyncio.run, so reuse dies with "Event loop is
    # closed" (#121). Both providers must hand the SDK an httpx client
    # with keep-alive off and the SDK-default 600s timeout.
    # A real subclass: the SDKs isinstance-check http_client against
    # httpx.AsyncClient, so a plain recording function breaks them.
    seen = []

    class _RecordingAsyncClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            seen.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("app.services.llm.httpx.AsyncClient", _RecordingAsyncClient)
    LLMService(api_key="k", provider=provider)

    (kwargs,) = seen
    assert kwargs["limits"].keepalive_expiry == 0.0
    assert kwargs["timeout"].connect == 600.0
    assert kwargs["timeout"].read == 600.0


@pytest.mark.parametrize("provider", ["claude", "muse"])
def test_providers_hand_sdk_the_configured_http_client(provider):
    service = LLMService(api_key="k", provider=provider)

    assert isinstance(service.client._client, httpx.AsyncClient)


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


def _status_error(cls, status_code):
    request = httpx.Request("POST", "https://example.test/v1/x")
    return cls("boom", response=httpx.Response(status_code, request=request), body=None)


@pytest.mark.parametrize("cls", [anthropic.APIStatusError, openai.APIStatusError])
@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_transient_status_errors(cls, status_code):
    assert _is_transient_llm_error(_status_error(cls, status_code)) is True


@pytest.mark.parametrize("cls", [anthropic.APIStatusError, openai.APIStatusError])
@pytest.mark.parametrize("status_code", [400, 401, 404])
def test_non_transient_status_errors(cls, status_code):
    assert _is_transient_llm_error(_status_error(cls, status_code)) is False


@pytest.fixture
def parity_shop(db_session):
    merchant = User(username="255700000300")
    db_session.add(merchant)
    db_session.commit()
    account = WhatsAppAccount(
        name="Parity Shop", phone_number_id="PARITY_PHONE", access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.add(Product(
        slug="parity-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    ))
    db_session.commit()
    return account


def _service_with_stubbed_complete(provider_name, captured):
    service = LLMService(api_key=None, provider=provider_name)
    service.client = object()  # truthy so generate_response doesn't short-circuit

    async def stubbed_complete(*, system, messages, max_tokens):
        captured["system"] = system
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        return "Kwa bei hiyo, nina ombi lingine. [LANGUAGE: sw]"

    service.provider.complete = stubbed_complete
    return service


async def test_system_prompt_identical_across_providers(db_session, parity_shop):
    """Both providers get the same system instruction (#49).

    generate_response builds one prompt regardless of backend; if a
    provider-conditional branch ever becomes necessary it must be a
    deliberate, reviewed change — not something that slips in silently.
    """
    captured_claude, captured_muse = {}, {}
    claude = _service_with_stubbed_complete("claude", captured_claude)
    muse = _service_with_stubbed_complete("muse", captured_muse)

    # Fresh phones on both sides so customer state and chat history match.
    await claude.generate_response(
        message_body="Bei ya mwisho ni ngapi?", from_phone="255711130200",
        db=db_session, account=parity_shop,
    )
    await muse.generate_response(
        message_body="Bei ya mwisho ni ngapi?", from_phone="255711130201",
        db=db_session, account=parity_shop,
    )

    assert captured_muse["system"] == captured_claude["system"]
    # Guard against vacuous equality: the shared prompt must carry the
    # buffered floor (10% above the 40,000 real floor).
    assert "44,000" in captured_muse["system"]
    assert captured_muse["max_tokens"] == captured_claude["max_tokens"] == 1024


async def test_openai_connection_error_raises_llm_unavailable_when_retrying(
    db_session, parity_shop, monkeypatch
):
    """A Muse outage retries the same way a Claude outage does (#38/#49)."""
    service = LLMService(api_key="test-model-key", provider="muse")

    async def raising_complete(*, system, messages, max_tokens):
        raise openai.APIConnectionError(
            request=httpx.Request("POST", "https://api.meta.ai/v1/chat/completions")
        )

    monkeypatch.setattr(service.provider, "complete", raising_complete)

    with pytest.raises(LLMUnavailable):
        await service.generate_response(
            message_body="hi", from_phone="255711130202", db=db_session,
            account=parity_shop, retry_on_outage=True,
        )


@pytest.fixture
def parity_ig_shop(db_session):
    """Merchant + IG account + product for DM/comment parity tests (#50)."""
    merchant = User(username="255700000301")
    db_session.add(merchant)
    db_session.commit()
    account = InstagramAccount(
        ig_business_id="ig_biz_parity", access_token="tok", username="parityshop",
        ai_enabled=True, user_id=merchant.id,
    )
    db_session.add(account)
    db_session.add(Product(
        slug="parity-ig-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    ))
    db_session.commit()
    return account


def _dm_service_with_stubbed_complete(provider_name, captured, reply_text="Hi! They're 50,000 TZS."):
    service = LLMService(api_key=None, provider=provider_name)
    service.client = object()  # truthy so generate_dm_response doesn't short-circuit

    async def stubbed_complete(*, system, messages, max_tokens):
        captured["system"] = system
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        return reply_text

    service.provider.complete = stubbed_complete
    return service


async def test_dm_system_prompt_identical_across_providers(db_session, parity_ig_shop):
    """Both providers get the same DM instruction (#50).

    generate_dm_response builds one prompt regardless of backend; if a
    provider-conditional branch ever becomes necessary it must be a
    deliberate, reviewed change — not something that slips in silently.
    """
    def fresh_customer(tag):
        c = InstagramCustomer(ig_scoped_id=f"igsid_parity_{tag}", username=f"parity_{tag}", account_id=parity_ig_shop.id)
        db_session.add(c)
        db_session.commit()
        ctx = InstagramConversationContext(customer_id=c.id)
        db_session.add(ctx)
        db_session.commit()
        return c, ctx

    captured_claude, captured_muse = {}, {}
    claude = _dm_service_with_stubbed_complete("claude", captured_claude)
    muse = _dm_service_with_stubbed_complete("muse", captured_muse)

    c1, ctx1 = fresh_customer("c1")
    await claude.generate_dm_response(
        "How much for the shoes?", db=db_session, account=parity_ig_shop,
        customer=c1, context=ctx1, conversation_history=[],
    )
    c2, ctx2 = fresh_customer("c2")
    await muse.generate_dm_response(
        "How much for the shoes?", db=db_session, account=parity_ig_shop,
        customer=c2, context=ctx2, conversation_history=[],
    )

    assert captured_muse["system"] == captured_claude["system"]
    # Guard against vacuous equality: the shared prompt must carry the
    # buffered floor (10% above the 40,000 real floor) and the phone-bearing
    # agreement format.
    assert "44,000" in captured_muse["system"]
    assert "phone=<phone number>" in captured_muse["system"]
    assert captured_muse["max_tokens"] == captured_claude["max_tokens"] == 500


def _comment_service_with_stubbed_complete(provider_name, captured):
    service = LLMService(api_key=None, provider=provider_name)
    service.client = object()  # truthy so generate_comment_reply doesn't short-circuit

    async def stubbed_complete(*, system, messages, max_tokens):
        captured["system"] = system
        captured["messages"] = messages
        captured["max_tokens"] = max_tokens
        return "PUBLIC: Thanks! Sent you a DM.\nPRIVATE: They're 50,000 TZS."

    service.provider.complete = stubbed_complete
    return service


async def test_comment_system_prompt_identical_across_providers(db_session, parity_ig_shop):
    """Both providers get the same comment instruction (#50).

    Same rationale as the WhatsApp/DM parity tests: one shared wording,
    no silent provider-conditional branches.
    """
    captured_claude, captured_muse = {}, {}
    claude = _comment_service_with_stubbed_complete("claude", captured_claude)
    muse = _comment_service_with_stubbed_complete("muse", captured_muse)

    await claude.generate_comment_reply(
        "How much?", merchant_id=parity_ig_shop.user_id, db=db_session,
    )
    await muse.generate_comment_reply(
        "How much?", merchant_id=parity_ig_shop.user_id, db=db_session,
    )

    assert captured_muse["system"] == captured_claude["system"]
    # Guard against vacuous equality: currency guardrail, off-topic
    # guidance (#50), and the PUBLIC:/PRIVATE: format must all be present.
    assert "Tanzanian Shillings" in captured_muse["system"]
    assert "has nothing to do with the shop" in captured_muse["system"]
    assert "PUBLIC:" in captured_muse["system"]
    assert captured_muse["max_tokens"] == captured_claude["max_tokens"] == 500
