"""add instagram_accounts.token_refreshed_at

Revision ID: 2a7e9c1d4b52
Revises: 1cf5e7cfbfd5
Create Date: 2026-10-31

Lazy IG token refresh (#80). Nullable: pre-existing rows read as stale and
get one opportunistic refresh on next use, then stamped.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2a7e9c1d4b52'
down_revision: Union[str, None] = '1cf5e7cfbfd5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'instagram_accounts',
        sa.Column('token_refreshed_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('instagram_accounts', 'token_refreshed_at')
