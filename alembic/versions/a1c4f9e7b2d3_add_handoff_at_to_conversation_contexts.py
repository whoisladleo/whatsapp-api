"""add_handoff_at_to_conversation_contexts

Revision ID: a1c4f9e7b2d3
Revises: e22aa687267d
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c4f9e7b2d3'
down_revision: Union[str, None] = 'e22aa687267d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('conversation_contexts', sa.Column('handoff_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('conversation_contexts', 'handoff_at')
