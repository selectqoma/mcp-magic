import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    capabilities: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    naics_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    certifications: Mapped[list[str] | None] = mapped_column(ARRAY(String))  # 8a, HUBZone, SDVOSB, WOSB, SB
    past_agencies: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    description: Mapped[str | None] = mapped_column(Text)
    webhook_url: Mapped[str | None] = mapped_column(String(500))  # Agency tier alerts
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
