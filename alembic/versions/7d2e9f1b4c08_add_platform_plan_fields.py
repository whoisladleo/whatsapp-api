"""add platform plan fields to products

Revision ID: 7d2e9f1b4c08
Revises: 9c1d4b87e2f0
Create Date: 2026-10-31

Dynamic plan SKUs (#90): is_platform_plan flags the products that gate
Amara access, monthly_reply_limit sets each plan's meter. The data step
flags the product named by PLATFORM_SUBSCRIPTION_PRODUCT_SLUG so the
existing Taskflow OS subscription becomes a plan on deploy; when the
setting is unset the migration is schema-only.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7d2e9f1b4c08'
down_revision: Union[str, None] = '9c1d4b87e2f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('products', sa.Column('is_platform_plan', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('products', sa.Column('monthly_reply_limit', sa.Integer(), nullable=True))
    from app.core.config import settings
    slug = (settings.PLATFORM_SUBSCRIPTION_PRODUCT_SLUG or "").strip()
    if slug:
        conn = op.get_bind()
        conn.execute(
            sa.text(
                "UPDATE products SET is_platform_plan = true "
                "WHERE slug = :slug AND is_platform_plan = false"
            ),
            {"slug": slug},
        )


def downgrade() -> None:
    op.drop_column('products', 'monthly_reply_limit')
    op.drop_column('products', 'is_platform_plan')
