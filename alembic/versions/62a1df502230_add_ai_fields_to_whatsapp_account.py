"""add_ai_fields_to_whatsapp_account

Revision ID: 62a1df502230
Revises: 002_add_brokerage
Create Date: 2026-05-03 11:59:21.694452

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '62a1df502230'
down_revision: Union[str, None] = '002_add_brokerage'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('whatsapp_accounts', sa.Column('product_slug', sa.String(), nullable=True))
    op.add_column('whatsapp_accounts', sa.Column('ai_enabled', sa.Boolean(), nullable=True, server_default='false'))
    op.add_column('whatsapp_accounts', sa.Column('system_prompt', sa.String(), nullable=True))

def downgrade() -> None:
    op.drop_column('whatsapp_accounts', 'system_prompt')
    op.drop_column('whatsapp_accounts', 'ai_enabled')
    op.drop_column('whatsapp_accounts', 'product_slug')
