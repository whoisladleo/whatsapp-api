"""add_phone_number_pool_table

Revision ID: f3b8d2c99a41
Revises: a1c4f9e7b2d3
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b8d2c99a41'
down_revision: Union[str, None] = 'a1c4f9e7b2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'phone_number_pool',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('phone_number_id', sa.String(), nullable=False),
        sa.Column('display_name', sa.String(), nullable=True),
        sa.Column(
            'status',
            sa.Enum('AVAILABLE', 'ASSIGNED', 'RELEASED', name='phonenumberstatus'),
            nullable=False,
            server_default='AVAILABLE',
        ),
        sa.Column('whatsapp_account_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('assigned_at', sa.DateTime(), nullable=True),
        sa.Column('released_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['whatsapp_account_id'], ['whatsapp_accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_phone_number_pool_id'), 'phone_number_pool', ['id'], unique=False)
    op.create_index(
        op.f('ix_phone_number_pool_phone_number_id'),
        'phone_number_pool',
        ['phone_number_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_phone_number_pool_phone_number_id'), table_name='phone_number_pool')
    op.drop_index(op.f('ix_phone_number_pool_id'), table_name='phone_number_pool')
    op.drop_table('phone_number_pool')
