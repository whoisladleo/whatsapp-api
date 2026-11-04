"""IG DM parity fields + IG platform flag (#136)

Revision ID: b136igparity
Revises: a3d5f7b9c1e2
Create Date: 2026-11-04

Gives InstagramConversationContext the same negotiation/quote columns the
WhatsApp ConversationContext already has (failed-cycle auto-handoff and
quote nudges need them), InstagramCustomer a language column (the model
classifies once per channel), and InstagramAccount an is_platform_account
flag (mirrors WhatsAppAccount — set once by hand for Taskflow's own
account, never inferred). All new columns except the flag are nullable so
existing rows need no backfill; readers treat NULL as unset.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b136igparity'
down_revision: Union[str, None] = 'a3d5f7b9c1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "instagram_conversation_contexts",
        sa.Column("failed_cycles", sa.Integer(), nullable=True),
    )
    op.add_column(
        "instagram_conversation_contexts",
        sa.Column("current_product_intent", sa.String(), nullable=True),
    )
    op.add_column(
        "instagram_conversation_contexts",
        sa.Column("last_offer", sa.Float(), nullable=True),
    )
    op.add_column(
        "instagram_conversation_contexts",
        sa.Column("pending_quote_price", sa.Float(), nullable=True),
    )
    op.add_column(
        "instagram_conversation_contexts",
        sa.Column("pending_quote_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "instagram_conversation_contexts",
        sa.Column("quote_nudge_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "instagram_customers",
        sa.Column("language", sa.String(), nullable=True),
    )
    op.add_column(
        "instagram_accounts",
        sa.Column("is_platform_account", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("instagram_accounts", "is_platform_account")
    op.drop_column("instagram_customers", "language")
    op.drop_column("instagram_conversation_contexts", "quote_nudge_count")
    op.drop_column("instagram_conversation_contexts", "pending_quote_at")
    op.drop_column("instagram_conversation_contexts", "pending_quote_price")
    op.drop_column("instagram_conversation_contexts", "last_offer")
    op.drop_column("instagram_conversation_contexts", "current_product_intent")
    op.drop_column("instagram_conversation_contexts", "failed_cycles")
