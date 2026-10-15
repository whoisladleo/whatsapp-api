"""add is_platform_account to whatsapp_accounts

Revision ID: 6979a066a92a
Revises: 6d014d16ea20
Create Date: 2026-10-10 16:49:31.104560

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6979a066a92a'
down_revision: Union[str, None] = '6d014d16ea20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "whatsapp_accounts",
        sa.Column("is_platform_account", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Backfill for any existing install still relying on the name match
    # this column replaces — the one row broken by a prior rename (see #21)
    # won't match this and is fixed by hand separately.
    op.execute(
        "UPDATE whatsapp_accounts SET is_platform_account = TRUE WHERE lower(name) = 'taskflow'"
    )


def downgrade() -> None:
    op.drop_column("whatsapp_accounts", "is_platform_account")
