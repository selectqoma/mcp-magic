import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class Award(Base):
    __tablename__ = "awards"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    award_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    recipient_name: Mapped[str | None] = mapped_column(String(500))
    recipient_uei: Mapped[str | None] = mapped_column(String(50))
    recipient_cage: Mapped[str | None] = mapped_column(String(20))
    agency: Mapped[str | None] = mapped_column(String(500))
    sub_agency: Mapped[str | None] = mapped_column(String(500))
    title: Mapped[str | None] = mapped_column(Text)
    naics_code: Mapped[str | None] = mapped_column(String(20))
    award_amount: Mapped[float | None] = mapped_column(Numeric(20, 2))
    award_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_of_performance_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    place_of_performance: Mapped[str | None] = mapped_column(String(500))
    raw_data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_awards_recipient_uei", "recipient_uei"),
        Index("idx_awards_recipient_name", "recipient_name"),
        Index("idx_awards_agency", "agency"),
        Index("idx_awards_naics", "naics_code"),
        Index("idx_awards_date", "award_date"),
    )
