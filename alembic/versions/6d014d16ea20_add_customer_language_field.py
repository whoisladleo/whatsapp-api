"""add customer language field

Revision ID: 6d014d16ea20
Revises: 0d99eb3bd075
Create Date: 2026-09-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6d014d16ea20'
down_revision: Union[str, None] = '0d99eb3bd075'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('customers', sa.Column('language', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('customers', 'language')
