"""add SUBSCRIPTION_NUDGE to amaraeventkind

Revision ID: f2c4e6a8b0d1
Revises: e4f7a2c9d1b5
Create Date: 2026-11-02

The subscription-nudge sweep (app/tasks/subscription_nudges.py) records
each send as an AmaraEvent, and those same rows drive the per-merchant
schedule — but Postgres native enums reject values added only in Python,
so the value needs this ALTER TYPE. IF NOT EXISTS keeps the CI
downgrade/upgrade round-trip of this revision green.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'f2c4e6a8b0d1'
down_revision: Union[str, None] = 'e4f7a2c9d1b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE amaraeventkind ADD VALUE IF NOT EXISTS 'SUBSCRIPTION_NUDGE'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; the unused value is harmless.
    pass
