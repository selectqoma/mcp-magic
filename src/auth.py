import secrets
from datetime import UTC, datetime

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
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def check_search_limit(user: User, session: AsyncSession) -> tuple[bool, str]:
    """Returns (allowed, reason). Uses atomic UPDATE to prevent race conditions."""
    now = datetime.now(UTC)

    limits = TIER_LIMITS[user.tier]
    if limits["searches_per_day"] == -1:
        return True, ""

    daily_limit = limits["searches_per_day"]

    # Single atomic UPDATE: reset on new day OR increment if under limit.
    # Uses a CASE expression so there is no window between reset and increment.
    result = await session.execute(
        text("""
            UPDATE users
            SET
                searches_today = CASE
                    WHEN searches_reset_at IS NULL OR DATE(searches_reset_at AT TIME ZONE 'UTC') < CURRENT_DATE
                    THEN 1
                    WHEN searches_today < :limit
                    THEN searches_today + 1
                    ELSE searches_today
                END,
                searches_reset_at = CASE
                    WHEN searches_reset_at IS NULL OR DATE(searches_reset_at AT TIME ZONE 'UTC') < CURRENT_DATE
                    THEN :now
                    ELSE searches_reset_at
                END
            WHERE id = :uid
              AND (
                    searches_reset_at IS NULL
                    OR DATE(searches_reset_at AT TIME ZONE 'UTC') < CURRENT_DATE
                    OR searches_today < :limit
              )
            RETURNING searches_today, searches_reset_at
        """),
        {"uid": str(user.id), "limit": daily_limit, "now": now},
    )
    await session.commit()

    row = result.fetchone()
    if row is None:
        # WHERE clause did not match — counter was at or above limit today
        return False, (
            f"Free tier limit reached ({daily_limit} searches/day). "
            "Upgrade to Pro at govconmcp.com for unlimited searches."
        )

    return True, ""


def requires_pro(user: User) -> tuple[bool, str]:
    if user.tier in (UserTier.PRO, UserTier.AGENCY):
        return True, ""
    return False, "This feature requires a Pro subscription. Upgrade at govconmcp.com."


def requires_agency(user: User) -> tuple[bool, str]:
    if user.tier == UserTier.AGENCY:
        return True, ""
    return False, "This feature requires an Agency subscription. Upgrade at govconmcp.com."
