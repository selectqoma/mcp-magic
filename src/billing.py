"""Paddle billing webhook handler and checkout helpers."""

import hashlib
import hmac
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.user import User, UserTier

logger = logging.getLogger(__name__)

TIER_BY_PADDLE_PRICE: dict[str, str] = {}  # populated from env at startup


def verify_paddle_webhook(payload: bytes, signature_header: str) -> bool:
    """Verify Paddle webhook signature (v2 format)."""
    if not settings.paddle_webhook_secret:
        logger.warning("PADDLE_WEBHOOK_SECRET not set — skipping verification (dev mode)")
        return True
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(";"))
        ts = parts.get("ts", "")
        h1 = parts.get("h1", "")
        signed_payload = f"{ts}:{payload.decode()}"
        expected = hmac.new(
            settings.paddle_webhook_secret.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, h1)
    except Exception as e:
        logger.error(f"Webhook signature verification failed: {e}")
        return False


async def handle_paddle_webhook(event_type: str, data: dict, session: AsyncSession) -> None:
    """Route Paddle webhook events to the appropriate handler."""
    handlers = {
        "subscription.created": _on_subscription_created,
        "subscription.updated": _on_subscription_updated,
        "subscription.canceled": _on_subscription_canceled,
        "subscription.past_due": _on_subscription_past_due,
    }
    handler = handlers.get(event_type)
    if handler:
        await handler(data, session)
    else:
        logger.debug(f"Unhandled Paddle event: {event_type}")


async def _on_subscription_created(data: dict, session: AsyncSession) -> None:
    user = await _find_user_by_paddle(data, session)
    if not user:
        return
    tier = _tier_from_subscription(data)
    user.tier = tier
    user.paddle_subscription_id = data.get("id")
    user.paddle_customer_id = data.get("customer_id")
    # Cancel trial
    user.trial_ends_at = None
    await session.commit()
    logger.info(f"User {user.email} subscribed → {tier}")


async def _on_subscription_updated(data: dict, session: AsyncSession) -> None:
    user = await _find_user_by_paddle(data, session)
    if not user:
        return
    tier = _tier_from_subscription(data)
    user.tier = tier
    await session.commit()
    logger.info(f"User {user.email} updated → {tier}")


async def _on_subscription_canceled(data: dict, session: AsyncSession) -> None:
    user = await _find_user_by_paddle(data, session)
    if not user:
        return
    user.tier = UserTier.FREE
    user.paddle_subscription_id = None
    await session.commit()
    logger.info(f"User {user.email} canceled → FREE")


async def _on_subscription_past_due(data: dict, session: AsyncSession) -> None:
    user = await _find_user_by_paddle(data, session)
    if not user:
        return
    # Downgrade to free on payment failure
    user.tier = UserTier.FREE
    await session.commit()
    logger.warning(f"User {user.email} past due → downgraded to FREE")


async def _find_user_by_paddle(data: dict, session: AsyncSession) -> User | None:
    customer_id = data.get("customer_id")
    email = data.get("customer", {}).get("email") if isinstance(data.get("customer"), dict) else None

    if customer_id:
        result = await session.execute(
            select(User).where(User.paddle_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user:
            return user

    if email:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    logger.warning(f"Could not find user for Paddle event: {data.get('id')}")
    return None


def _tier_from_subscription(data: dict) -> str:
    """Infer tier from Paddle subscription data."""
    items = data.get("items", [])
    for item in items:
        price_id = item.get("price", {}).get("id", "") if isinstance(item.get("price"), dict) else ""
        if "agency" in price_id.lower() or "agency" in str(item).lower():
            return UserTier.AGENCY
        if "pro" in price_id.lower() or "pro" in str(item).lower():
            return UserTier.PRO
    # Fallback: check amount
    total = data.get("recurring_transaction_details", {}).get("totals", {}).get("subtotal", "0")
    try:
        amount = int(str(total).replace(".", ""))
        if amount >= 19900:  # $199
            return UserTier.AGENCY
        if amount >= 4900:   # $49
            return UserTier.PRO
    except (ValueError, TypeError):
        pass
    return UserTier.PRO
