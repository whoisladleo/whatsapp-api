import os

# The suite signs/verifies JWTs symmetrically and never needs a real
# SECRET_KEY — but Settings refuses to construct with a missing/placeholder
# key outside test mode (fail-fast for misconfigured deploys). Set TESTING
# before any app.* import so the module-level `settings = Settings()` sees it.
os.environ["TESTING"] = "true"

import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.rate_limit as rate_limit_module
from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import Base, User
from app.db.session import get_db

# Rate limiting uses a real redis client by default; swap it for an in-memory
# fake for the whole test session so tests never need a running Redis and
# rate-limit counters can be deterministically reset between tests.
rate_limit_module._redis_client = fakeredis.FakeRedis()

# process_inbound_message (app/tasks/webhook_processing.py) is dispatched via
# apply_async from the webhook handler. Rather than stub that out like the
# handoff reminder task below, run it eagerly (synchronously, in-process, no
# broker needed) so webhook integration tests can keep asserting on its side
# effects (Message rows, WhatsApp sends) immediately after client.post()
# returns, instead of every such test having to call the task directly.
# task_eager_propagates surfaces bugs inside the task as a loud test failure
# rather than a silently-wrong assertion.
celery_app.conf.task_always_eager = True
celery_app.conf.task_eager_propagates = True

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture(autouse=True)
def _reset_state():
    rate_limit_module._redis_client.flushall()
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_real_external_credentials(monkeypatch):
    """
    Forces R2/WhatsApp-app-secret style settings off by default so tests never
    accidentally hit real external services because of ambient environment
    variables the process happens to have (e.g. real R2 credentials exported
    in the shell, not just `.env`). A previous run of this suite made a real
    upload to production Cloudflare R2 before this fixture existed, precisely
    because `settings.R2_ACCOUNT_ID` was truthy from the shell environment.
    Tests that want to exercise the R2 path must explicitly monkeypatch these
    back on top of a mocked boto3 client.
    """
    monkeypatch.setattr(settings, "R2_ACCOUNT_ID", None)
    monkeypatch.setattr(settings, "R2_ACCESS_KEY_ID", None)
    monkeypatch.setattr(settings, "R2_SECRET_ACCESS_KEY", None)
    monkeypatch.setattr(settings, "R2_BUCKET_NAME", None)
    monkeypatch.setattr(settings, "R2_PUBLIC_URL", None)
    # Same reasoning, now that real Twilio credentials also live in `.env`
    # (see app/services/twilio_provisioning.py / pool_provisioning.py) —
    # tests exercising those paths must explicitly monkeypatch a fake
    # Twilio client back on top rather than ever touching the real API.
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", None)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    # Same for the Meta catalog: product-write hooks dispatch sync_catalog_item
    # (eager in tests), which must silently no-op rather than hit Meta.
    monkeypatch.setattr(settings, "CATALOG_ID", None)
    monkeypatch.setattr(settings, "CATALOG_ACCESS_TOKEN", None)
    # Webhook signature secrets stay unset in tests, and webhooks now fail
    # closed without them (#81) — the suite is the dev case, so allow
    # unsigned payloads file-wide. Tests for the fail-closed behavior itself
    # pin this back to False explicitly.
    monkeypatch.setattr(settings, "ALLOW_UNSIGNED_WEBHOOKS", True)
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", None)
    monkeypatch.setattr(settings, "INSTAGRAM_APP_SECRET", None)
    monkeypatch.setattr(settings, "TUNZAA_SECRET_KEY", None)


@pytest.fixture(autouse=True)
def _no_real_celery_broker(monkeypatch):
    """
    Some request paths schedule follow-up work via Celery's apply_async(...)
    at event time (e.g. the handoff reminder scheduled from llm.py, the
    soft-nudge/scarcity-nudge/auto-cancel reminders scheduled at order
    creation in LLMService._close_deal, and the location-capture task
    dispatched from the webhook handler on an inbound location message).
    Without a real broker, apply_async
    would attempt a real Redis connection — and running these eagerly would
    also re-hit the same nested-event-loop problem process_inbound_message's
    _run_async exists to solve, since scheduling happens from within an
    already-running loop. Stub them to no-ops by default; a test that wants
    to assert scheduling happened can re-patch locally to something that
    records the call, and task *bodies* are still tested directly (called
    synchronously, not via apply_async) in their respective test modules.
    """
    from app.tasks.handoff_reminders import send_handoff_reminder
    from app.tasks.order_reminders import auto_cancel_order, send_scarcity_nudge, send_soft_nudge
    from app.tasks.location_capture import capture_customer_location
    from app.tasks.quote_nudges import send_quote_nudge
    from app.tasks.product_subscription_billing import (
        charge_renewal as charge_product_subscription_renewal,
        check_past_due_grace_expired,
        retry_installment_reconcile,
    )

    monkeypatch.setattr(send_handoff_reminder, "apply_async", lambda *a, **kw: None)
    monkeypatch.setattr(send_soft_nudge, "apply_async", lambda *a, **kw: None)
    monkeypatch.setattr(send_scarcity_nudge, "apply_async", lambda *a, **kw: None)
    monkeypatch.setattr(auto_cancel_order, "apply_async", lambda *a, **kw: None)
    monkeypatch.setattr(capture_customer_location, "apply_async", lambda *a, **kw: None)
    monkeypatch.setattr(check_past_due_grace_expired, "apply_async", lambda *a, **kw: None)
    monkeypatch.setattr(send_quote_nudge, "apply_async", lambda *a, **kw: None)
    monkeypatch.setattr(charge_product_subscription_renewal, "apply_async", lambda *a, **kw: None)
    monkeypatch.setattr(retry_installment_reconcile, "apply_async", lambda *a, **kw: None)
    from app.tasks.usage_alerts import send_usage_warning
    monkeypatch.setattr(send_usage_warning, "apply_async", lambda *a, **kw: None)
    # The subscription-nudge sweep is dispatched from every verified webhook;
    # without this it would run inline in eager mode on every webhook test.
    from app.tasks.subscription_nudges import send_subscription_nudges
    monkeypatch.setattr(send_subscription_nudges, "apply_async", lambda *a, **kw: None)


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _webhook_task_uses_test_session(monkeypatch, db_session):
    """
    process_inbound_message calls the module-level SessionLocal directly
    (it's a Celery task, not a FastAPI route, so get_db's dependency
    override doesn't apply) — redirect it to the same session the test uses,
    same as the order_reminders/handoff_reminders task tests already do.
    """
    from app.tasks import webhook_processing
    monkeypatch.setattr(webhook_processing, "SessionLocal", lambda: db_session)


@pytest.fixture
def client(db_session):
    from main import app

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def auth_user(db_session):
    """Creates a User and returns (user, auth_headers)."""
    user = User(username="255700000001")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    headers = {"Authorization": f"Bearer {create_access_token(user.id)}"}
    return user, headers
