"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\"")

    op.create_table(
        "opportunities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("source_id", sa.String(255), nullable=False, unique=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("agency", sa.String(500)),
        sa.Column("naics_codes", postgresql.ARRAY(sa.String)),
        sa.Column("set_aside_type", sa.String(100)),
        sa.Column("posted_date", sa.DateTime(timezone=True)),
        sa.Column("response_deadline", sa.DateTime(timezone=True)),
        sa.Column("estimated_value_low", sa.Numeric(20, 2)),
        sa.Column("estimated_value_high", sa.Numeric(20, 2)),
        sa.Column("place_of_performance", sa.String(500)),
        sa.Column("contact_info", postgresql.JSONB),
        sa.Column("attachments", postgresql.JSONB),
        sa.Column("requirements", postgresql.JSONB),
        sa.Column("raw_data", postgresql.JSONB),
        sa.Column("embedding", Vector(1536)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("idx_opps_source_id", "opportunities", ["source", "source_id"])
    op.create_index("idx_opps_deadline", "opportunities", ["response_deadline"])
    op.create_index("idx_opps_naics", "opportunities", ["naics_codes"], postgresql_using="gin")
    # ivfflat index for fast cosine similarity search — must be created AFTER data is loaded
    # Run manually after initial sync: CREATE INDEX idx_opps_embedding ON opportunities
    # USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
    # (Skipped here because ivfflat requires rows to exist first)

    op.create_table(
        "awards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("award_id", sa.String(255), nullable=False, unique=True),
        sa.Column("recipient_name", sa.String(500)),
        sa.Column("recipient_uei", sa.String(50)),
        sa.Column("recipient_cage", sa.String(20)),
        sa.Column("agency", sa.String(500)),
        sa.Column("sub_agency", sa.String(500)),
        sa.Column("title", sa.Text),
        sa.Column("naics_code", sa.String(20)),
        sa.Column("award_amount", sa.Numeric(20, 2)),
        sa.Column("award_date", sa.DateTime(timezone=True)),
        sa.Column("period_of_performance_end", sa.DateTime(timezone=True)),
        sa.Column("place_of_performance", sa.String(500)),
        sa.Column("raw_data", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )
    op.create_index("idx_awards_recipient_uei", "awards", ["recipient_uei"])
    op.create_index("idx_awards_recipient_name", "awards", ["recipient_name"])
    op.create_index("idx_awards_agency", "awards", ["agency"])
    op.create_index("idx_awards_naics", "awards", ["naics_code"])
    op.create_index("idx_awards_date", "awards", ["award_date"])

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("api_key", sa.String(64), nullable=False, unique=True),
        sa.Column("tier", sa.String(20), nullable=False, server_default="free"),
        sa.Column("paddle_customer_id", sa.String(100)),
        sa.Column("paddle_subscription_id", sa.String(100)),
        sa.Column("searches_today", sa.Integer, server_default="0"),
        sa.Column("searches_reset_at", sa.DateTime(timezone=True)),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "company_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.String)),
        sa.Column("naics_codes", postgresql.ARRAY(sa.String)),
        sa.Column("certifications", postgresql.ARRAY(sa.String)),
        sa.Column("past_agencies", postgresql.ARRAY(sa.String)),
        sa.Column("description", sa.Text),
        sa.Column("webhook_url", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )

    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("company_profiles.id")),
        sa.Column("opportunity_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("opportunities.id")),
        sa.Column("match_score", sa.Float),
        sa.Column("read", sa.Boolean, server_default="false"),
        sa.Column("matched_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint("profile_id", "opportunity_id", name="uq_notification_profile_opp"),
    )


def downgrade() -> None:
    op.drop_table("notifications")
    op.drop_table("company_profiles")
    op.drop_table("users")
    op.drop_table("awards")
    op.drop_table("opportunities")
