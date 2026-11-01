"""scope conversation contexts per whatsapp account

Revision ID: c8d3e5a1f2b4
Revises: b4e2f7a1c9d3
Create Date: 2026-11-01

ConversationContext was one row per customer phone globally, so handoff
and quote state leaked across shops: a handoff at shop A silenced the
bot at shop B for the same phone, and chatting at B cleared A's pending
quote (killing A's nudge cascade). This adds a nullable account_id FK
and re-keys uniqueness from (customer_id) to (customer_id, account_id).

Legacy rows keep account_id NULL and are adopted lazily by the first
account that sees them (see crud.message.get_or_create_context) — no
backfill here, since only live traffic can attribute a row to the right
shop, and NULLs never collide under the new unique constraint on
Postgres. The downgrade re-imposes the single-column unique, so it only
succeeds when no customer has more than one context row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8d3e5a1f2b4'
down_revision: Union[str, None] = 'b4e2f7a1c9d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('conversation_contexts', sa.Column('account_id', sa.Uuid(), nullable=True))
    op.create_foreign_key(
        'fk_conversation_contexts_account_id',
        'conversation_contexts', 'whatsapp_accounts',
        ['account_id'], ['id'],
    )
    op.create_index(
        op.f('ix_conversation_contexts_account_id'),
        'conversation_contexts', ['account_id'], unique=False,
    )
    # Auto-named by the unnamed UniqueConstraint('customer_id') in
    # d19df75ee8ad/0d99eb3bd075 — Postgres renders that as
    # conversation_contexts_customer_id_key.
    op.drop_constraint('conversation_contexts_customer_id_key', 'conversation_contexts', type_='unique')
    op.create_unique_constraint(
        'uq_conversation_contexts_customer_account',
        'conversation_contexts', ['customer_id', 'account_id'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_conversation_contexts_customer_account', 'conversation_contexts', type_='unique'
    )
    op.create_unique_constraint(
        'conversation_contexts_customer_id_key', 'conversation_contexts', ['customer_id']
    )
    op.drop_index(op.f('ix_conversation_contexts_account_id'), table_name='conversation_contexts')
    op.drop_constraint('fk_conversation_contexts_account_id', 'conversation_contexts', type_='foreignkey')
    op.drop_column('conversation_contexts', 'account_id')
