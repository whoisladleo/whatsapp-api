"""Add Message model and callback_url

Revision ID: 002_add_brokerage
Revises: 001_initial
Create Date: 2026-04-21 17:56:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "002_add_brokerage"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add callback_url to whatsapp_accounts
    op.add_column(
        "whatsapp_accounts", sa.Column("callback_url", sa.String(), nullable=True)
    )

    # Create messages table
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("whatsapp_message_id", sa.String(), nullable=True),
        sa.Column("from_phone", sa.String(), nullable=True),
        sa.Column("to_phone", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("body", sa.String(), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("direction", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("timestamp", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["whatsapp_accounts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_messages_id"), "messages", ["id"], unique=False)
    op.create_index(
        op.f("ix_messages_whatsapp_message_id"),
        "messages",
        ["whatsapp_message_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_whatsapp_message_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_column("whatsapp_accounts", "callback_url")
