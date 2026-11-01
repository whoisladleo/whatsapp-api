import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_database_url_uses_explicit_override_when_set():
    settings = Settings(SQLALCHEMY_DATABASE_URI="postgresql://explicit/uri")

    assert settings.database_url == "postgresql://explicit/uri"


def test_database_url_builds_from_parts_when_no_override():
    settings = Settings(
        SQLALCHEMY_DATABASE_URI=None,
        POSTGRES_USER="u",
        POSTGRES_PASSWORD="p",
        POSTGRES_SERVER="host",
        POSTGRES_PORT="5432",
        POSTGRES_DB="db",
    )

    assert settings.database_url == "postgresql://u:p@host:5432/db"


def test_placeholder_secret_key_rejected_when_not_testing():
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(SECRET_KEY="your-super-secret-key-change-it", TESTING=False)


def test_empty_secret_key_rejected_when_not_testing():
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(SECRET_KEY="", TESTING=False)


def test_documented_example_keys_rejected_when_not_testing():
    # .env.example and the docker-compose.prod.yml fallback are public too —
    # a deploy that never set a real key must not boot on those either.
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(SECRET_KEY="generate_a_random_string_here", TESTING=False)
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(SECRET_KEY="generate_a_very_secure_random_string_here", TESTING=False)


def test_real_secret_key_accepted_when_not_testing():
    settings = Settings(SECRET_KEY="a-real-random-secret-value", TESTING=False)

    assert settings.SECRET_KEY == "a-real-random-secret-value"


def test_testing_mode_allows_placeholder_secret_key():
    settings = Settings(SECRET_KEY="your-super-secret-key-change-it", TESTING=True)

    assert settings.SECRET_KEY == "your-super-secret-key-change-it"
