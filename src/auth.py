import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user import TIER_LIMITS, User, UserTier


def generate_api_key() -> str:
    return secrets.token_urlsafe(48)


async def get_user_by_api_key(api_key: str, session: AsyncSession) -> User | None:
    result = await session.execute(select(User).where(User.api_key == api_key))
    return result.scalar_one_or_none()


async def create_user(email: str, session: AsyncSession) -> User:
    user = User(
        email=email,
        api_key=generate_api_key(),
        tier=UserTier.FREE,
        trial_ends_at=datetime.now(UTC) + timedelta(days=14),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def check_search_limit(user: User, session: AsyncSession) -> tuple[bool, str]:
    """Returns (allowed, reason). Resets daily counter if needed."""
    now = datetime.now(UTC)

    # Reset counter if it's a new day
    if user.searches_reset_at is None or user.searches_reset_at.date() < now.date():
        user.searches_today = 0
        user.searches_reset_at = now
        await session.commit()

    # Check trial (treat as pro during trial)
    effective_tier = user.tier
    if user.trial_ends_at and user.trial_ends_at > now and user.tier == UserTier.FREE:
        effective_tier = UserTier.PRO

    limits = TIER_LIMITS[effective_tier]
    if limits["searches_per_day"] == -1:
        return True, ""

    if user.searches_today >= limits["searches_per_day"]:
        return False, (
            f"Free tier limit reached ({limits['searches_per_day']} searches/day). "
            "Upgrade to Pro at mcpmagic.io for unlimited searches."
        )

    user.searches_today += 1
    await session.commit()
    return True, ""


def requires_pro(user: User) -> tuple[bool, str]:
    now = datetime.now(UTC)
    in_trial = user.trial_ends_at and user.trial_ends_at > now
    if user.tier in (UserTier.PRO, UserTier.AGENCY) or in_trial:
        return True, ""
    return False, "This feature requires a Pro subscription. Upgrade at mcpmagic.io."


def requires_agency(user: User) -> tuple[bool, str]:
    if user.tier == UserTier.AGENCY:
        return True, ""
    return False, "This feature requires an Agency subscription. Upgrade at mcpmagic.io."
