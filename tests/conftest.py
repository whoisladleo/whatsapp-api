import fakeredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.core.rate_limit as rate_limit_module
from app.core.config import settings
from app.core.security import create_access_token
from app.db.models import Base, User
from app.db.session import get_db

# Rate limiting uses a real redis client by default; swap it for an in-memory
# fake for the whole test session so tests never need a running Redis and
# rate-limit counters can be deterministically reset between tests.
rate_limit_module._redis_client = fakeredis.FakeRedis()

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


@pytest.fixture(autouse=True)
def _no_real_celery_broker(monkeypatch):
    """
    Some request paths schedule follow-up work via Celery's apply_async(...)
    at event time (e.g. the handoff reminder scheduled from llm.py). Without
    a real broker, apply_async would attempt a real Redis connection. Stub
    it to a no-op by default; a test that wants to assert scheduling
    happened can re-patch it locally to something that records the call.
    """
    from app.tasks.handoff_reminders import send_handoff_reminder
    monkeypatch.setattr(send_handoff_reminder, "apply_async", lambda *a, **kw: None)


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


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
