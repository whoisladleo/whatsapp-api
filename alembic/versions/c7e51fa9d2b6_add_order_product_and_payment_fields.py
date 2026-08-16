"""add_order_product_and_payment_fields

Revision ID: c7e51fa9d2b6
Revises: f3b8d2c99a41
Create Date: 2026-08-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e51fa9d2b6'
down_revision: Union[str, None] = 'f3b8d2c99a41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('product_id', sa.Integer(), nullable=True))
    op.add_column('orders', sa.Column('account_id', sa.Integer(), nullable=True))
    op.add_column('orders', sa.Column('quantity', sa.Integer(), nullable=True, server_default='1'))
    op.add_column(
        'orders',
        sa.Column(
            'payment_type',
            sa.Enum('FULL', 'INSTALLMENT', name='paymenttype'),
            nullable=True,
        ),
    )
    op.add_column('orders', sa.Column('tunzaa_transaction_id', sa.String(), nullable=True))
    op.create_foreign_key(
        'fk_orders_product_id_products', 'orders', 'products', ['product_id'], ['id']
    )
    op.create_foreign_key(
        'fk_orders_account_id_whatsapp_accounts',
        'orders',
        'whatsapp_accounts',
        ['account_id'],
        ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('fk_orders_account_id_whatsapp_accounts', 'orders', type_='foreignkey')
    op.drop_constraint('fk_orders_product_id_products', 'orders', type_='foreignkey')
    op.drop_column('orders', 'tunzaa_transaction_id')
    op.drop_column('orders', 'payment_type')
    op.drop_column('orders', 'quantity')
    op.drop_column('orders', 'account_id')
    op.drop_column('orders', 'product_id')
