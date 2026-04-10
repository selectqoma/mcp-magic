"""billing integrity: webhook idempotency + paddle_customer_id unique index

Revision ID: 002
Revises: 001
Create Date: 2026-04-10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Unique index on paddle_customer_id — prevents duplicate customer linkage
    op.create_index(
        "idx_users_paddle_customer_id",
        "users",
        ["paddle_customer_id"],
        unique=True,
        postgresql_where=sa.text("paddle_customer_id IS NOT NULL"),
    )

    # Webhook events table — ensures each Paddle event is processed exactly once
    op.create_table(
        "paddle_webhook_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("event_id", sa.String(255), nullable=False, unique=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("idx_paddle_events_event_id", "paddle_webhook_events", ["event_id"], unique=True)


def downgrade() -> None:
    op.drop_table("paddle_webhook_events")
    op.drop_index("idx_users_paddle_customer_id", table_name="users")
