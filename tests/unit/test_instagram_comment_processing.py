import pytest

import app.tasks.instagram_comment_processing as task_module
from app.db.models import InstagramAccount, InstagramComment, User


@pytest.fixture(autouse=True)
def _use_test_session(monkeypatch, db_session):
    """Tasks call the module-level SessionLocal directly, not FastAPI's get_db."""
    monkeypatch.setattr(task_module, "SessionLocal", lambda: db_session)


@pytest.fixture
def owner(db_session):
    user = User(username="255700000900")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def account(db_session, owner):
    acct = InstagramAccount(
        ig_business_id="ig_biz_1",
        access_token="ig-tok",
        username="shopowner",
        ai_enabled=True,
        user_id=owner.id,
    )
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    return acct


@pytest.fixture
def comment(db_session, account):
    c = InstagramComment(
        account_id=account.id,
        comment_id="comment_1",
        media_id="media_1",
        from_username="commenter1",
        from_ig_scoped_id="igsid_1",
        body="how much?",
    )
    db_session.add(c)
    db_session.commit()
    db_session.refresh(c)
    return c


class _FakeProvider:
    def __init__(self, access_token, public_response=None, private_response=None):
        self.access_token = access_token
        self.public_response = public_response or {"id": "reply_1"}
        self.private_response = private_response or {"message_id": "msg_1"}
        self.public_calls = []
        self.private_calls = []

    async def reply_to_comment(self, comment_id, message):
        self.public_calls.append((comment_id, message))
        return self.public_response

    async def send_private_reply(self, ig_business_id, comment_id, message):
        self.private_calls.append((ig_business_id, comment_id, message))
        return self.private_response


@pytest.fixture
def fake_llm_reply(monkeypatch):
    async def fake_generate_comment_reply(**kwargs):
        fake_generate_comment_reply.called_with = kwargs
        return {"public_reply": "Sent you a DM!", "private_reply": "It's 45000 TZS."}

    fake_generate_comment_reply.called_with = None

    class FakeLLMService:
        generate_comment_reply = staticmethod(fake_generate_comment_reply)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())
    return fake_generate_comment_reply


def test_missing_account_or_comment_is_a_no_op(db_session):
    result = task_module.process_inbound_comment(999999, 999999)
    assert result == "Account or comment not found."


def test_llm_unavailable_is_a_no_op(db_session, account, comment, monkeypatch):
    monkeypatch.setattr(task_module, "llm_service", None)
    comment_id = comment.id

    result = task_module.process_inbound_comment(account.id, comment_id)

    assert result == "LLM unavailable."
    reloaded = db_session.query(InstagramComment).filter(InstagramComment.id == comment_id).first()
    assert reloaded.public_reply_sent is False
    assert reloaded.private_reply_sent is False


def test_sends_public_and_private_replies_on_success(
    db_session, account, comment, fake_llm_reply, monkeypatch
):
    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(
        task_module, "InstagramProvider", lambda access_token: fake_provider
    )

    account_id, comment_id, merchant_id = account.id, comment.id, account.user_id
    result = task_module.process_inbound_comment(account_id, comment_id)

    assert result == "Processed."
    assert fake_provider.public_calls == [("comment_1", "Sent you a DM!")]
    assert fake_provider.private_calls == [("ig_biz_1", "comment_1", "It's 45000 TZS.")]

    reloaded = db_session.query(InstagramComment).filter(InstagramComment.id == comment_id).first()
    assert reloaded.public_reply_sent is True
    assert reloaded.private_reply_sent is True

    assert fake_llm_reply.called_with["comment_body"] == "how much?"
    assert fake_llm_reply.called_with["merchant_id"] == merchant_id
    assert fake_llm_reply.called_with["account_name"] == "shopowner"


def test_no_reply_generated_sends_nothing(db_session, account, comment, monkeypatch):
    async def fake_generate_comment_reply(**kwargs):
        return None

    class FakeLLMService:
        generate_comment_reply = staticmethod(fake_generate_comment_reply)

    monkeypatch.setattr(task_module, "llm_service", FakeLLMService())

    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    result = task_module.process_inbound_comment(account.id, comment.id)

    assert result == "No reply generated."
    assert fake_provider.public_calls == []
    assert fake_provider.private_calls == []


def test_public_reply_failure_still_attempts_private_reply(
    db_session, account, comment, fake_llm_reply, monkeypatch
):
    fake_provider = _FakeProvider(
        "ig-tok", public_response={"error": {"message": "rate limited"}}
    )
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    account_id, comment_id = account.id, comment.id
    task_module.process_inbound_comment(account_id, comment_id)

    assert fake_provider.private_calls == [("ig_biz_1", "comment_1", "It's 45000 TZS.")]
    reloaded = db_session.query(InstagramComment).filter(InstagramComment.id == comment_id).first()
    assert reloaded.public_reply_sent is False
    assert reloaded.private_reply_sent is True


def test_private_reply_failure_does_not_affect_public_reply_flag(
    db_session, account, comment, fake_llm_reply, monkeypatch
):
    fake_provider = _FakeProvider(
        "ig-tok", private_response={"error": {"message": "already replied"}}
    )
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    account_id, comment_id = account.id, comment.id
    task_module.process_inbound_comment(account_id, comment_id)

    reloaded = db_session.query(InstagramComment).filter(InstagramComment.id == comment_id).first()
    assert reloaded.public_reply_sent is True
    assert reloaded.private_reply_sent is False


def test_public_and_private_replies_are_each_metered(db_session, account, comment, fake_llm_reply, monkeypatch):
    from app.db.models import AmaraEvent, AmaraEventKind

    monkeypatch.setattr(
        task_module, "InstagramProvider",
        lambda access_token: _FakeProvider("ig-tok", public_response={"error": {"message": "nope"}}),
    )
    account_id, comment_id, merchant_id = account.id, comment.id, account.user_id

    task_module.process_inbound_comment(account_id, comment_id)

    # Only the private reply got through, so only it is billable (#41).
    replies = db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).all()
    assert [(r.merchant_id, r.channel) for r in replies] == [(merchant_id, "instagram_comment")]


def test_both_comment_replies_metered_when_both_succeed(db_session, account, comment, fake_llm_reply, monkeypatch):
    from app.db.models import AmaraEvent, AmaraEventKind

    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: _FakeProvider("ig-tok"))

    task_module.process_inbound_comment(account.id, comment.id)

    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).count() == 2


def test_quota_exhausted_stays_silent_on_comments(db_session, account, comment, fake_llm_reply, monkeypatch):
    """No public holding message — an exhausted meter stays silent on
    comments rather than advertising the limit. The merchant already got
    their 100% warning (#90)."""
    from datetime import datetime, timedelta

    import app.tasks.usage_alerts as usage_alerts
    from app.db.models import (
        AmaraEvent, AmaraEventKind, Customer, Order, OrderStatus, PaymentType,
        Product, ProductSubscription, ProductSubscriptionStatus,
    )
    from app.services import usage

    owner = db_session.query(User).filter(User.id == account.user_id).one()
    seller = User(username="255700000901")
    db_session.add(seller)
    db_session.commit()
    plan = Product(
        slug="plan-quota", name="Plan", max_price=100000, min_price=100000,
        quantity=0, merchant_id=seller.id, is_subscription=True,
        is_platform_plan=True, monthly_reply_limit=2,
    )
    buyer = Customer(phone_number=owner.username)
    db_session.add_all([plan, buyer])
    db_session.commit()
    order = Order(
        customer_id=buyer.id, product_id=plan.id, quantity=1, agreed_price=100000,
        payment_type=PaymentType.FULL, status=OrderStatus.PAID,
    )
    db_session.add(order)
    db_session.commit()
    db_session.add(ProductSubscription(
        order_id=order.id, customer_id=buyer.id, product_id=plan.id,
        status=ProductSubscriptionStatus.ACTIVE, monthly_amount=100000,
        next_billing_at=datetime.utcnow() + timedelta(days=5),
    ))
    db_session.commit()
    monkeypatch.setattr(usage_alerts.send_usage_warning, "apply_async", lambda args, **kw: None)
    usage.record_reply(db_session, owner.id, "whatsapp")
    usage.record_reply(db_session, owner.id, "whatsapp")

    fake_provider = _FakeProvider("ig-tok")
    monkeypatch.setattr(task_module, "InstagramProvider", lambda access_token: fake_provider)

    result = task_module.process_inbound_comment(account.id, comment.id)

    assert result == "Reply quota exhausted; staying silent."
    assert fake_provider.public_calls == []
    assert fake_provider.private_calls == []
    assert fake_llm_reply.called_with is None
    assert db_session.query(AmaraEvent).filter_by(kind=AmaraEventKind.REPLY).count() == 2
