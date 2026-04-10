import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.user import TIER_LIMITS, User, UserTier


def generate_api_key() -> str:
    return secrets.token_urlsafe(48)


async def get_user_by_api_key(api_key: str, session: AsyncSession) -> User | None:
    from sqlalchemy import select
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
    """Returns (allowed, reason). Uses atomic UPDATE to prevent race conditions."""
    now = datetime.now(UTC)

    # Check trial (treat as pro during trial)
    effective_tier = user.tier
    if user.trial_ends_at and user.trial_ends_at.replace(tzinfo=UTC) > now and user.tier == UserTier.FREE:
        effective_tier = UserTier.PRO

    limits = TIER_LIMITS[effective_tier]
    if limits["searches_per_day"] == -1:
        return True, ""

    daily_limit = limits["searches_per_day"]

    # Reset counter if new day — atomic update
    reset_result = await session.execute(
        update(User)
        .where(
            User.id == user.id,
            text("DATE(searches_reset_at AT TIME ZONE 'UTC') < CURRENT_DATE"),
        )
        .values(searches_today=0, searches_reset_at=now)
        .returning(User.searches_today)
    )
    await session.commit()

    # Atomic increment — only succeeds if under limit
    result = await session.execute(
        update(User)
        .where(User.id == user.id, User.searches_today < daily_limit)
        .values(searches_today=User.searches_today + 1)
        .returning(User.searches_today)
    )
    await session.commit()

    row = result.fetchone()
    if row is None:
        # Counter was at or above limit — fetch current count for error message
        return False, (
            f"Free tier limit reached ({daily_limit} searches/day). "
            "Upgrade to Pro at mcpmagic.io for unlimited searches."
        )

    return True, ""


def requires_pro(user: User) -> tuple[bool, str]:
    now = datetime.now(UTC)
    trial_end = user.trial_ends_at
    if trial_end and trial_end.tzinfo is None:
        trial_end = trial_end.replace(tzinfo=UTC)
    in_trial = trial_end is not None and trial_end > now
    if user.tier in (UserTier.PRO, UserTier.AGENCY) or in_trial:
        return True, ""
    return False, "This feature requires a Pro subscription. Upgrade at mcpmagic.io."


def requires_agency(user: User) -> tuple[bool, str]:
    if user.tier == UserTier.AGENCY:
        return True, ""
    return False, "This feature requires an Agency subscription. Upgrade at mcpmagic.io."
