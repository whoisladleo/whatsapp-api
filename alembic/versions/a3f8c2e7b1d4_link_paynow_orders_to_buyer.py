"""link channel-less orders to the buying merchant (#16)

Revision ID: a3f8c2e7b1d4
Revises: e7f1a3c4d9b2
Create Date: 2026-11-01

Dashboard-native purchases (Pay Now) carry no channel account, so the
merchant join in get_orders_by_merchant matches neither side and the rows
are invisible to everyone — the Orders page showed only account-linked
(chat) orders. New rows record the buyer at creation
(orders.buyer_user_id, set by pay_now); this migration backfills old
channel-less rows by matching the buyer customer's phone against user
phone numbers/usernames. Only unambiguous matches link (exactly one
matching user); ambiguous or unmatched rows stay NULL — hidden, as today.
The downgrade drops the column and index; the links are unrecoverable.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f8c2e7b1d4'
down_revision: Union[str, None] = 'e7f1a3c4d9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('buyer_user_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_orders_buyer_user_id', 'orders', 'users', ['buyer_user_id'], ['id'])
    op.create_index('ix_orders_buyer_user_id', 'orders', ['buyer_user_id'])
    op.execute(
        """
        DO $$
        DECLARE
            linked_count integer;
        BEGIN
            UPDATE orders o
            SET buyer_user_id = sub.uid
            FROM (
                -- MIN over a text cast: Postgres defines no min(uuid), and
                -- the HAVING below guarantees a single distinct id anyway.
                SELECT o2.id AS oid, MIN(u.id::text)::uuid AS uid
                FROM orders o2
                JOIN customers c ON c.id = o2.customer_id
                JOIN users u ON (u.phone_number = c.phone_number OR u.username = c.phone_number)
                WHERE o2.account_id IS NULL
                  AND o2.instagram_account_id IS NULL
                  AND o2.buyer_user_id IS NULL
                GROUP BY o2.id
                HAVING COUNT(DISTINCT u.id) = 1
            ) sub
            WHERE o.id = sub.oid;
            GET DIAGNOSTICS linked_count = ROW_COUNT;
            RAISE NOTICE '#16 backfill: linked % channel-less orders to buying users', linked_count;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_index('ix_orders_buyer_user_id', table_name='orders')
    op.drop_constraint('fk_orders_buyer_user_id', 'orders', type_='foreignkey')
    op.drop_column('orders', 'buyer_user_id')
