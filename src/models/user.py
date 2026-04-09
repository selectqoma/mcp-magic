import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


class UserTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    AGENCY = "agency"


TIER_LIMITS = {
    UserTier.FREE: {"searches_per_day": 20, "profiles": 0, "pdf_parsing": False, "competitors": False},
    UserTier.PRO: {"searches_per_day": -1, "profiles": 3, "pdf_parsing": True, "competitors": True},
    UserTier.AGENCY: {"searches_per_day": -1, "profiles": 20, "pdf_parsing": True, "competitors": True},
}


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    api_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default=UserTier.FREE)
    paddle_customer_id: Mapped[str | None] = mapped_column(String(100))
    paddle_subscription_id: Mapped[str | None] = mapped_column(String(100))
    searches_today: Mapped[int] = mapped_column(Integer, default=0)
    searches_reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
