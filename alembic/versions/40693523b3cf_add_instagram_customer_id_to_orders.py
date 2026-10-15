"""add instagram_customer_id to orders

Revision ID: 40693523b3cf
Revises: f5dbaba27ad0
Create Date: 2026-10-15 22:16:33.600357

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '40693523b3cf'
down_revision: Union[str, None] = 'f5dbaba27ad0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('instagram_customer_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_orders_instagram_customer_id_instagram_customers',
        'orders', 'instagram_customers', ['instagram_customer_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'fk_orders_instagram_customer_id_instagram_customers', 'orders', type_='foreignkey'
    )
    op.drop_column('orders', 'instagram_customer_id')
