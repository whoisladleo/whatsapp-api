"""encrypt stored meta tokens and number pins at rest

Revision ID: e4f7a2c9d1b5
Revises: d9e3f5a1c7b2
Create Date: 2026-11-01

Meta access tokens and Cloud API two-step PINs lived in the database as
plaintext: any database read (backup, breach) yielded working credentials
for every merchant's WhatsApp and Instagram. From here on the app reads
and writes these columns through EncryptedString (Fernet, keyed by
TOKEN_ENCRYPTION_KEY), so this migration encrypts every existing value in
place. No DDL type change — ciphertext stays VARCHAR.

Self-contained on purpose: it reads TOKEN_ENCRYPTION_KEY straight from the
environment and does its own Fernet calls rather than importing app code,
so it keeps working no matter how the app evolves. Run order is safe:
scripts/run.sh migrates before starting the app, and downgrade restores
plaintext, so old code never reads ciphertext.

Second change: InstagramAccount.access_token becomes nullable, and stored
"" (the old "disconnected" marker) becomes NULL — randomized ciphertext
can't be matched with `!= ""` in SQL anymore, so code now filters
IS NOT NULL instead (see disconnect_account).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4f7a2c9d1b5'
down_revision: Union[str, None] = 'd9e3f5a1c7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (table, column, blank_to_null): blank_to_null converts "" to NULL instead
# of encrypting it (the old disconnected marker — only instagram used it).
_COLUMNS = (
    ("whatsapp_accounts", "access_token", False),
    ("instagram_accounts", "access_token", True),
    ("phone_number_pool", "two_step_pin", False),
    ("phone_number_provisioning_attempts", "two_step_pin", False),
)


def _fernet_from_env():
    import os

    from cryptography.fernet import Fernet

    raw = (os.environ.get("TOKEN_ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set: this migration encrypts "
            "stored tokens and cannot run without it."
        )
    try:
        return Fernet(raw.encode())
    except Exception:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not a valid Fernet key: refusing to "
            "encrypt stored tokens with it."
        )


def upgrade() -> None:
    op.alter_column(
        "instagram_accounts", "access_token",
        existing_type=sa.String(), nullable=True,
    )

    fernet = _fernet_from_env()
    bind = op.get_bind()
    for table, column, blank_to_null in _COLUMNS:
        rows = bind.execute(
            sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")
        ).all()
        for row_id, value in rows:
            if value == "" and blank_to_null:
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = NULL WHERE id = :id"),
                    {"id": row_id},
                )
            elif value:
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),
                    {"id": row_id, "value": fernet.encrypt(value.encode()).decode()},
                )
            # NULL and (non-instagram) "" carry no secret — left as-is.


def downgrade() -> None:
    fernet = _fernet_from_env()
    bind = op.get_bind()
    for table, column, _blank_to_null in _COLUMNS:
        rows = bind.execute(
            sa.text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")
        ).all()
        for row_id, value in rows:
            if value:
                bind.execute(
                    sa.text(f"UPDATE {table} SET {column} = :value WHERE id = :id"),
                    {"id": row_id, "value": fernet.decrypt(value.encode()).decode()},
                )

    bind.execute(sa.text("UPDATE instagram_accounts SET access_token = '' WHERE access_token IS NULL"))
    op.alter_column(
        "instagram_accounts", "access_token",
        existing_type=sa.String(), nullable=False,
    )
