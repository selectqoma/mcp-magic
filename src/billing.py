"""Paddle billing webhook handler and checkout helpers."""

import hashlib
import hmac
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.models.user import User, UserTier

logger = logging.getLogger(__name__)

WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300  # 5 minutes — replay protection


def verify_paddle_webhook(payload: bytes, signature_header: str) -> bool:
    """Verify Paddle webhook signature (v2 format). Fails CLOSED on any error."""
    if not settings.paddle_webhook_secret:
        # Hard fail in production — do not accept unsigned events
        logger.error("PADDLE_WEBHOOK_SECRET is not set. Rejecting webhook.")
        return False
    if not signature_header:
        logger.warning("Paddle webhook received with no signature header.")
        return False
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(";") if "=" in p)
        ts = parts.get("ts", "")
        h1 = parts.get("h1", "")
        if not ts or not h1:
            logger.warning("Malformed Paddle-Signature header.")
            return False

        # Replay protection: reject events older than tolerance window
        try:
            event_time = int(ts)
            if abs(time.time() - event_time) > WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS:
                logger.warning(f"Paddle webhook timestamp too old/future: {ts}")
                return False
        except ValueError:
            logger.warning(f"Non-integer timestamp in Paddle-Signature: {ts}")
            return False

        signed_payload = f"{ts}:{payload.decode('utf-8')}"
        expected = hmac.new(
            settings.paddle_webhook_secret.encode(),
            signed_payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, h1)

    except Exception as e:
        logger.error(f"Paddle webhook signature verification error: {e}")
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
    user = await _find_user_by_paddle_customer_id(data, session)
    if not user:
        logger.error(f"subscription.created: no user found for customer_id={data.get('customer_id')}")
        return
    tier = _tier_from_subscription(data)
    user.tier = tier
    user.paddle_subscription_id = data.get("id")
    user.paddle_customer_id = data.get("customer_id")
    user.trial_ends_at = None  # end trial on paid subscription
    await session.commit()
    logger.info(f"User {user.email} subscribed → {tier}")


async def _on_subscription_updated(data: dict, session: AsyncSession) -> None:
    user = await _find_user_by_paddle_customer_id(data, session)
    if not user:
        return
    tier = _tier_from_subscription(data)
    user.tier = tier
    await session.commit()
    logger.info(f"User {user.email} updated → {tier}")


async def _on_subscription_canceled(data: dict, session: AsyncSession) -> None:
    user = await _find_user_by_paddle_customer_id(data, session)
    if not user:
        return
    user.tier = UserTier.FREE
    user.paddle_subscription_id = None
    user.trial_ends_at = None  # clear trial so they can't reactivate it
    await session.commit()
    logger.info(f"User {user.email} canceled → FREE")


async def _on_subscription_past_due(data: dict, session: AsyncSession) -> None:
    user = await _find_user_by_paddle_customer_id(data, session)
    if not user:
        return
    user.tier = UserTier.FREE
    await session.commit()
    logger.warning(f"User {user.email} past due → downgraded to FREE")


async def _find_user_by_paddle_customer_id(data: dict, session: AsyncSession) -> User | None:
    """Look up user ONLY by paddle_customer_id — never by email from webhook payload."""
    customer_id = data.get("customer_id")
    if not customer_id:
        logger.error(f"Paddle event has no customer_id: {data.get('id', 'unknown')}")
        return None
    result = await session.execute(
        select(User).where(User.paddle_customer_id == customer_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        logger.error(f"No user found for paddle_customer_id={customer_id}")
    return user


async def link_paddle_customer(user_id, customer_id: str, session: AsyncSession) -> None:
    """Call this when creating a Paddle checkout to link customer_id to user before webhook fires."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.paddle_customer_id = customer_id
        await session.commit()


def _tier_from_subscription(data: dict) -> str:
    """Infer tier from Paddle subscription data. Defaults to FREE on ambiguity."""
    items = data.get("items", [])
    for item in items:
        price = item.get("price", {}) if isinstance(item.get("price"), dict) else {}
        price_id = price.get("id", "").lower()
        price_name = price.get("name", "").lower()
        if "agency" in price_id or "agency" in price_name:
            return UserTier.AGENCY
        if "pro" in price_id or "pro" in price_name:
            return UserTier.PRO
    # Fallback by amount — but default FREE not PRO on ambiguity
    try:
        total = data.get("recurring_transaction_details", {}).get("totals", {}).get("subtotal", "0")
        amount = int(str(total).replace(".", ""))
        if amount >= 19900:
            return UserTier.AGENCY
        if amount >= 4900:
            return UserTier.PRO
    except (ValueError, TypeError, AttributeError):
        pass
    logger.warning(f"Could not determine tier from subscription {data.get('id')} — defaulting FREE")
    return UserTier.FREE
