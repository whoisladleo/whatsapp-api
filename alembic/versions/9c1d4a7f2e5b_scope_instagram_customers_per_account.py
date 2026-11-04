"""scope instagram customers per account

Revision ID: 9c1d4a7f2e5b
Revises: b136igparity
Create Date: 2026-11-04

InstagramCustomer.ig_scoped_id was globally unique, but IGSIDs are
app-scoped: the same person messaging two shops has the same ig_scoped_id
at both, so the second shop was handed the first shop's row — leaking DM
history and conversation state across shops. This re-keys uniqueness from
(ig_scoped_id) to (account_id, ig_scoped_id), mirroring c8d3e5a1f2b4's
(customer_id, account_id) scoping for WhatsApp contexts.

Current data holds at most one row per ig_scoped_id globally, so the new
constraint can't fail on duplicates. But historical mingled rows (one row
whose DMs actually came from two shops) are NOT unmingled here — the row
keeps whichever account_id it already had, and only live traffic can
attribute history to the right shop. OWNER ACTION: inspect
instagram_customers rows whose account's DMs reference another shop's
threads and re-attribute by hand if needed.

The downgrade re-imposes the single-column unique, so it only succeeds
when no ig_scoped_id spans more than one account.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '9c1d4a7f2e5b'
down_revision: Union[str, None] = 'b136igparity'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f('ix_instagram_customers_ig_scoped_id'), table_name='instagram_customers')
    op.create_unique_constraint(
        'uq_instagram_customers_account_igsid',
        'instagram_customers', ['account_id', 'ig_scoped_id'],
    )
    op.create_index(
        op.f('ix_instagram_customers_ig_scoped_id'),
        'instagram_customers', ['ig_scoped_id'], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_instagram_customers_ig_scoped_id'), table_name='instagram_customers')
    op.drop_constraint(
        'uq_instagram_customers_account_igsid', 'instagram_customers', type_='unique'
    )
    op.create_index(
        op.f('ix_instagram_customers_ig_scoped_id'),
        'instagram_customers', ['ig_scoped_id'], unique=True,
    )
