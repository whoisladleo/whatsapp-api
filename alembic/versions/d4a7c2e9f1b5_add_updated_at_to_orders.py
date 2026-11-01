"""add updated_at to orders

Revision ID: d4a7c2e9f1b5
Revises: c9e1f4a2b7d3
Create Date: 2026-11-01

#113: the payment-resend path throttles repeat Tunzaa prompts off
Order.updated_at. Nullable, no backfill (NULL = predates this; the
throttle fails open for those rows).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4a7c2e9f1b5'
down_revision: Union[str, None] = 'c9e1f4a2b7d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('updated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'updated_at')
