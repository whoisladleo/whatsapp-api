"""add_tunzaa_plan_id_and_delivery_coords

Revision ID: d4e6a1f8c3b9
Revises: c7e51fa9d2b6
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e6a1f8c3b9'
down_revision: Union[str, None] = 'c7e51fa9d2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('tunzaa_plan_id', sa.String(), nullable=True))
    op.add_column('customers', sa.Column('delivery_latitude', sa.Float(), nullable=True))
    op.add_column('customers', sa.Column('delivery_longitude', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'delivery_longitude')
    op.drop_column('customers', 'delivery_latitude')
    op.drop_column('orders', 'tunzaa_plan_id')
