"""add_quote_nudge_fields

Revision ID: f2a8c1d94e7b
Revises: b7f2a91e6c4d
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a8c1d94e7b'
down_revision: Union[str, None] = 'b7f2a91e6c4d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('conversation_contexts', sa.Column('pending_quote_price', sa.Float(), nullable=True))
    op.add_column('conversation_contexts', sa.Column('pending_quote_at', sa.DateTime(), nullable=True))
    op.add_column('conversation_contexts', sa.Column('quote_nudge_count', sa.Integer(), nullable=True, server_default='0'))


def downgrade() -> None:
    op.drop_column('conversation_contexts', 'quote_nudge_count')
    op.drop_column('conversation_contexts', 'pending_quote_at')
    op.drop_column('conversation_contexts', 'pending_quote_price')
