"""add explicit handoff alert number to users

Revision ID: b7c3e9a1f4d2
Revises: 7d2e9f1b4c08
Create Date: 2026-11-01

Dedicated handoff number (#105): nullable, no default, no index (never
queried by it — read off the already-loaded merchant row), no backfill
(unset means OTP username, then profile phone, as before).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7c3e9a1f4d2'
down_revision: Union[str, None] = '7d2e9f1b4c08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('handoff_phone_number', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'handoff_phone_number')
