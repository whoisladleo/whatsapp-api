"""Transparent Fernet encryption for DB columns holding third-party secrets.

`EncryptedString` is a SQLAlchemy column type that reads/writes plaintext in
Python while storing Fernet ciphertext in the database, so a database read
(backup, breach, curious admin) no longer yields working Meta tokens or
number PINs. All existing attribute access keeps working unchanged — no call
site needs to know the column is encrypted.

The key comes from settings.TOKEN_ENCRYPTION_KEY (a `Fernet.generate_key()`
value); config refuses to boot a real process without one. Tests use a fixed
insecure key via Settings.TESTING. Decryption is strict: a value that fails
to decrypt raises instead of passing ciphertext through as a token.
"""

from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from app.core.config import settings


# Fixed test-only key (32 zero bytes in valid Fernet encoding) — used only
# when Settings.TESTING is true. Never valid in a real deploy: config's
# fail-fast validator rejects booting without a real TOKEN_ENCRYPTION_KEY.
_TEST_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

_fernet = None


def _get_fernet():
    """Process-wide Fernet built from config on first use (the key is fixed
    per process, so caching is safe)."""
    # Local import: cryptography is a hard dependency, but keeping the import
    # here makes the failure mode a clear error at first DB use rather than
    # at module import time.
    from cryptography.fernet import Fernet

    global _fernet
    if _fernet is None:
        raw = _TEST_KEY if settings.TESTING else (settings.TOKEN_ENCRYPTION_KEY or "")
        _fernet = Fernet(raw.strip().encode())
    return _fernet


def encrypt_str(value: str) -> str:
    return _get_fernet().encrypt(value.encode()).decode()


def decrypt_str(value: str) -> str:
    return _get_fernet().decrypt(value.encode()).decode()


class EncryptedString(TypeDecorator):
    """VARCHAR column stored as Fernet ciphertext. None passes through as
    None on both sides; every other value is encrypted on write and
    decrypted on read. Same VARCHAR type as before, so no DDL type change."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt_str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt_str(value)
