import pytest

from app.db.models import Product, User, WhatsAppAccount


@pytest.fixture(autouse=True)
def _pin_claude_provider(monkeypatch):
    """These tests stub an Anthropic-shaped client, so pin the provider they
    exercise to Claude regardless of ambient LLM_PROVIDER."""
    monkeypatch.setattr("app.services.llm.settings.LLM_PROVIDER", "claude")


@pytest.fixture
def merchant(db_session):
    owner = User(username="255700000200")
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


@pytest.fixture
def shop_account(db_session, merchant):
    account = WhatsAppAccount(
        name="Test Shop",
        phone_number_id="LLM_PHONE",
        access_token="tok",
        user_id=merchant.id,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account


@pytest.fixture
def shoes(db_session, merchant):
    p = Product(
        slug="deal-shoes", name="Shoes", max_price=50000, min_price=40000,
        quantity=3, merchant_id=merchant.id,
    )
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture
def platform_account(db_session, merchant):
    account = WhatsAppAccount(
        name="Taskflow",
        phone_number_id="PLATFORM_PHONE",
        access_token="tok",
        user_id=merchant.id,
        is_platform_account=True,
    )
    db_session.add(account)
    db_session.commit()
    db_session.refresh(account)
    return account
