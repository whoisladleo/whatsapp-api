"""add installment config to products

Revision ID: f5dbaba27ad0
Revises: 6979a066a92a
Create Date: 2026-10-15 21:44:54.822838

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f5dbaba27ad0'
down_revision: Union[str, None] = '6979a066a92a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # See c7e51fa9d2b6: a bare sa.Enum(...) passed to op.add_column doesn't
    # emit CREATE TYPE on Postgres — the type must be created explicitly
    # first, or this fails in production with `type "installmentmode" does
    # not exist` (SQLite's test suite wouldn't catch this).
    installment_mode_enum = postgresql.ENUM('BNPL', 'SNPL', name='installmentmode')
    installment_mode_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        'products',
        sa.Column('allow_installments', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'products',
        sa.Column(
            'installment_mode', installment_mode_enum, nullable=False, server_default='BNPL'
        ),
    )


def downgrade() -> None:
    op.drop_column('products', 'installment_mode')
    op.drop_column('products', 'allow_installments')
    postgresql.ENUM(name='installmentmode').drop(op.get_bind(), checkfirst=True)
