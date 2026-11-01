"""one pending order per customer+product (#114)

Revision ID: e7f1a3c4d9b2
Revises: d4a7c2e9f1b5
Create Date: 2026-11-01

Sandbox data showed two closes 16ms apart both minting PENDING orders for
the same customer+product: the reclose guard's read-then-write check can't
see a concurrent close. This makes the invariant structural with a partial
unique index. The preamble retires any pre-existing duplicate PENDING sets
(keeping the latest per customer+product+payment-type group, restoring
their stock exactly as auto-cancel would) so index creation can't fail on
old data; the downgrade drops the index only — the preamble's cancels
stand.

Non-concurrent CREATE INDEX: the orders table is tiny and migrations run
at deploy startup before traffic is served, so no CONCURRENTLY needed
(which would also forbid running inside the migration transaction).
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e7f1a3c4d9b2'
down_revision: Union[str, None] = 'd4a7c2e9f1b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            restored_products integer;
            cancelled_orders integer;
        BEGIN
            -- Release the reserved units first, while the duplicate rows
            -- are still identifiable as PENDING (subscriptions claim no
            -- stock, so only non-subscription products restore).
            UPDATE products p
            SET quantity = COALESCE(p.quantity, 0) + sub.units
            FROM (
                SELECT o.product_id, SUM(COALESCE(o.quantity, 1)) AS units
                FROM orders o
                JOIN products pr ON pr.id = o.product_id
                WHERE o.status = 'PENDING'
                  AND o.product_id IS NOT NULL
                  AND NOT pr.is_subscription
                  AND o.id NOT IN (
                      SELECT DISTINCT ON (customer_id, product_id, payment_type) id
                      FROM orders WHERE status = 'PENDING'
                      ORDER BY customer_id, product_id, payment_type, created_at DESC NULLS LAST, id DESC
                  )
                GROUP BY o.product_id
            ) sub
            WHERE p.id = sub.product_id;
            GET DIAGNOSTICS restored_products = ROW_COUNT;

            -- Then retire every PENDING order that isn't its group's keeper.
            UPDATE orders o
            SET status = 'CANCELLED'
            WHERE o.status = 'PENDING'
              AND o.id NOT IN (
                  SELECT DISTINCT ON (customer_id, product_id, payment_type) id
                  FROM orders WHERE status = 'PENDING'
                  ORDER BY customer_id, product_id, payment_type, created_at DESC NULLS LAST, id DESC
              );
            GET DIAGNOSTICS cancelled_orders = ROW_COUNT;

            RAISE NOTICE '#114 dedupe: cancelled % duplicate PENDING orders, restored stock on % product rows',
                cancelled_orders, restored_products;
        END $$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ix_orders_one_pending_per_deal
        ON orders (customer_id, product_id, payment_type)
        WHERE status = 'PENDING'
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_orders_one_pending_per_deal")
