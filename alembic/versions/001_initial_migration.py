"""initial migration

Revision ID: 001_initial
Replaces: 
Create Date: 2026-04-21 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create users table
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("hashed_password", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_id"), "users", ["id"], unique=False)
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)

    # Create whatsapp_accounts table
    op.create_table(
        "whatsapp_accounts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("phone_number_id", sa.String(), nullable=False),
        sa.Column("access_token", sa.String(), nullable=False),
        sa.Column("verify_token", sa.String(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_whatsapp_accounts_id"), "whatsapp_accounts", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_whatsapp_accounts_phone_number_id"),
        "whatsapp_accounts",
        ["phone_number_id"],
        unique=True,
    )

    # Create media_entries table
    op.create_table(
        "media_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("media_id", sa.String(), nullable=False),
        sa.Column("file_name", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["whatsapp_accounts.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_media_entries_id"), "media_entries", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_media_entries_id"), table_name="media_entries")
    op.drop_table("media_entries")
    op.drop_index(
        op.f("ix_whatsapp_accounts_phone_number_id"), table_name="whatsapp_accounts"
    )
    op.drop_index(op.f("ix_whatsapp_accounts_id"), table_name="whatsapp_accounts")
    op.drop_table("whatsapp_accounts")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_index(op.f("ix_users_id"), table_name="users")
    op.drop_table("users")
