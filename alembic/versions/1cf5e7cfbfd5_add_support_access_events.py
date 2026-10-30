"""add support_access_events audit ledger

Revision ID: 1cf5e7cfbfd5
Revises: f207af6aefc4
Create Date: 2026-10-30

Support impersonation audit ledger (#75). Ids intentionally have no FKs:
users can be hard-deleted and an audit record must outlive them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1cf5e7cfbfd5'
down_revision: Union[str, None] = 'f207af6aefc4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'support_access_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('operator_id', sa.Uuid(), nullable=False),
        sa.Column('operator_label', sa.String(), nullable=True),
        sa.Column('target_id', sa.Uuid(), nullable=False),
        sa.Column('target_label', sa.String(), nullable=True),
        sa.Column(
            'kind',
            sa.Enum('START', 'END', 'WRITE', name='supportaccesskind'),
            nullable=False,
        ),
        sa.Column('method', sa.String(), nullable=True),
        sa.Column('path', sa.String(), nullable=True),
        sa.Column('detail', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_support_access_events_id', 'support_access_events', ['id'])
    op.create_index('ix_support_access_target_created', 'support_access_events', ['target_id', 'created_at'])
    op.create_index('ix_support_access_operator_created', 'support_access_events', ['operator_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_support_access_operator_created', table_name='support_access_events')
    op.drop_index('ix_support_access_target_created', table_name='support_access_events')
    op.drop_index('ix_support_access_events_id', table_name='support_access_events')
    op.drop_table('support_access_events')
    sa.Enum(name='supportaccesskind').drop(op.get_bind(), checkfirst=True)
