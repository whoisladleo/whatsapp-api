"""add processed_at to instagram_direct_messages

Revision ID: 132355fb22d3
Revises: 40693523b3cf
Create Date: 2026-10-15 22:29:55.359111

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '132355fb22d3'
down_revision: Union[str, None] = '40693523b3cf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('instagram_direct_messages', sa.Column('processed_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('instagram_direct_messages', 'processed_at')
