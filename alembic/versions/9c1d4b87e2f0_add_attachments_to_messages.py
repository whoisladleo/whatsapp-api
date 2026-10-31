"""add attachments to messages + instagram_direct_messages

Revision ID: 9c1d4b87e2f0
Revises: 2a7e9c1d4b52
Create Date: 2026-10-31

Normalized media for forwarded/shared content (#87). Nullable: pre-existing
rows simply have no attachments — WA rows hydrate from raw_data on read,
old IG rows render a "shared content" fallback.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9c1d4b87e2f0'
down_revision: Union[str, None] = '2a7e9c1d4b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'messages',
        sa.Column('attachments', sa.JSON(), nullable=True),
    )
    op.add_column(
        'instagram_direct_messages',
        sa.Column('attachments', sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('instagram_direct_messages', 'attachments')
    op.drop_column('messages', 'attachments')
