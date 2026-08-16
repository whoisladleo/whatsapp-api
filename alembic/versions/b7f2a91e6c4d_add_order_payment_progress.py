"""add_order_payment_progress

Revision ID: b7f2a91e6c4d
Revises: d4e6a1f8c3b9
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f2a91e6c4d'
down_revision: Union[str, None] = 'd4e6a1f8c3b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('paid_amount', sa.Float(), nullable=True))
    op.add_column('orders', sa.Column('remaining_balance', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'remaining_balance')
    op.drop_column('orders', 'paid_amount')
