"""orders product link sets null on product delete

Revision ID: a4e8b3d1c6f7
Revises: 9c1d4a7f2e5b
Create Date: 2026-11-04

Deleting a product with order history 500ed on Postgres (plain FK), while
every read path already tolerates a NULL product ("your order" fallbacks).
This re-issues fk_orders_product_id_products with ON DELETE SET NULL so
order history survives the product. crud.product.delete_product NULLs the
link explicitly too, since SQLite (tests) doesn't enforce FK actions —
both backends behave identically either way.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'a4e8b3d1c6f7'
down_revision: Union[str, None] = '9c1d4a7f2e5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('fk_orders_product_id_products', 'orders', type_='foreignkey')
    op.create_foreign_key(
        'fk_orders_product_id_products', 'orders', 'products',
        ['product_id'], ['id'], ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('fk_orders_product_id_products', 'orders', type_='foreignkey')
    op.create_foreign_key(
        'fk_orders_product_id_products', 'orders', 'products',
        ['product_id'], ['id'],
    )
