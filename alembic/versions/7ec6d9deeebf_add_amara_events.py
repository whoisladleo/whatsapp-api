"""add amara_events

Revision ID: 7ec6d9deeebf
Revises: 132355fb22d3
Create Date: 2026-10-23 10:12:13.593471

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7ec6d9deeebf'
down_revision: Union[str, None] = '132355fb22d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'amara_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('merchant_id', sa.Uuid(), nullable=False),
        sa.Column(
            'kind',
            sa.Enum('REPLY', 'QUOTE', 'HANDOFF', 'PAYMENT', 'USAGE_WARNING', name='amaraeventkind'),
            nullable=False,
        ),
        sa.Column('channel', sa.String(), nullable=True),
        sa.Column('account_id', sa.Uuid(), nullable=True),
        sa.Column('instagram_account_id', sa.Uuid(), nullable=True),
        sa.Column('product_name', sa.String(), nullable=True),
        sa.Column('amount', sa.Float(), nullable=True),
        sa.Column('detail', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['merchant_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_amara_events_id', 'amara_events', ['id'])
    op.create_index('ix_amara_events_merchant_kind_created', 'amara_events', ['merchant_id', 'kind', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_amara_events_merchant_kind_created', table_name='amara_events')
    op.drop_index('ix_amara_events_id', table_name='amara_events')
    op.drop_table('amara_events')
    sa.Enum(name='amaraeventkind').drop(op.get_bind(), checkfirst=True)
