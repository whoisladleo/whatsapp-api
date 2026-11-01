"""add users.trial_ends_at + trial audit kinds

Revision ID: a3d5f7b9c1e2
Revises: f2c4e6a8b0d1
Create Date: 2026-11-02

Operator-granted free trials (demo/trial access): a nullable timestamp on
the user that is_merchant_allowed_ai honors while in the future. Plus the
two SupportAccessEvent kinds the grant/revoke endpoints audit with —
Postgres native enums reject Python-only values, so they need ALTER TYPE
like SUBSCRIPTION_NUDGE did (IF NOT EXISTS keeps the CI round-trip green).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3d5f7b9c1e2'
down_revision: Union[str, None] = 'f2c4e6a8b0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("trial_ends_at", sa.DateTime(), nullable=True))
    op.execute("ALTER TYPE supportaccesskind ADD VALUE IF NOT EXISTS 'TRIAL_GRANTED'")
    op.execute("ALTER TYPE supportaccesskind ADD VALUE IF NOT EXISTS 'TRIAL_REVOKED'")


def downgrade() -> None:
    op.drop_column("users", "trial_ends_at")
    # Postgres cannot drop enum values; the unused values are harmless.
