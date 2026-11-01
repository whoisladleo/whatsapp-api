"""record number-purchase refund outcome

Revision ID: c9e1f4a2b7d3
Revises: b7c3e9a1f4d2
Create Date: 2026-11-01

Audit fix #110: record post-provisioning-failure refund state (MANUAL
= charge owed back, support reverses by hand), so the dashboard stops
statically claiming "you've been refunded". Nullable, no backfill (NULL
= nothing charged, or predates this).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9e1f4a2b7d3'
down_revision: Union[str, None] = 'b7c3e9a1f4d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('number_subscriptions', sa.Column('refund_status', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('number_subscriptions', 'refund_status')
