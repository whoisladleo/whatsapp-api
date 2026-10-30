"""add is_draft to products

Revision ID: f207af6aefc4
Revises: 7ec6d9deeebf
Create Date: 2026-10-30

Shop-import drafts (#74): items that fail mapping (missing title or
price) persist as inactive drafts the merchant completes in the
dashboard instead of being dropped. Metadata-only ADD COLUMN with a
server default — no table rewrite on Postgres 11+.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f207af6aefc4'
down_revision: Union[str, None] = '7ec6d9deeebf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'products',
        sa.Column('is_draft', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('products', 'is_draft')
