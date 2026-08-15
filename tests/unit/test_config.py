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
