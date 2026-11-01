"""add refund_status to orders

Revision ID: b4e2f7a1c9d3
Revises: a3f8c2e7b1d4
Create Date: 2026-11-01

Audit fix P6: a late COMPLETED with no stock to reclaim takes real money
while leaving the order FAILED/CANCELLED — record the refund-owed state
(MANUAL = charge owed back, support reverses by hand) instead of only
logging, mirroring number_subscriptions.refund_status (c9e1f4a2b7d3).
Nullable, no backfill (NULL = nothing owed, or predates this).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4e2f7a1c9d3'
down_revision: Union[str, None] = 'a3f8c2e7b1d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('refund_status', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'refund_status')
