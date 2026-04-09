import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source: Mapped[str] = mapped_column(String(20), nullable=False)       # sam | grants | sbir
    source_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    agency: Mapped[str | None] = mapped_column(String(500))
    naics_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    set_aside_type: Mapped[str | None] = mapped_column(String(100))
    posted_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    estimated_value_low: Mapped[float | None] = mapped_column(Numeric(20, 2))
    estimated_value_high: Mapped[float | None] = mapped_column(Numeric(20, 2))
    place_of_performance: Mapped[str | None] = mapped_column(String(500))
    contact_info: Mapped[dict | None] = mapped_column(JSONB)
    attachments: Mapped[list | None] = mapped_column(JSONB)   # [{filename, url, parsed: bool}]
    requirements: Mapped[dict | None] = mapped_column(JSONB)  # parsed PDF requirements
    raw_data: Mapped[dict | None] = mapped_column(JSONB)
    embedding: Mapped[list | None] = mapped_column(Vector(1536))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_opps_source_id", "source", "source_id"),
        Index("idx_opps_deadline", "response_deadline"),
        Index("idx_opps_naics", "naics_codes", postgresql_using="gin"),
        Index(
            "idx_opps_embedding",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
