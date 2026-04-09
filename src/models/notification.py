import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("company_profiles.id"))
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"))
    match_score: Mapped[float | None] = mapped_column(Float)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
