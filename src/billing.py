"""Paddle billing webhook handler and checkout helpers."""

import hashlib
import hmac
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.email import send_payment_failed, send_payment_receipt
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
    """Route Paddle webhook events to the appropriate handler.

    Uses event_id for idempotency — duplicate deliveries from Paddle are silently ignored.
    """
    from sqlalchemy import text as sa_text

    event_id = data.get("id", "")
    if event_id:
        # Insert event_id; if it already exists (duplicate), skip processing
        try:
            await session.execute(
                sa_text(
                    "INSERT INTO paddle_webhook_events (event_id, event_type) VALUES (:eid, :etype)"
                ),
                {"eid": event_id, "etype": event_type},
            )
            await session.commit()
        except Exception:
            # Unique violation → already processed
            await session.rollback()
            logger.info(f"Duplicate Paddle event ignored: {event_id}")
            return

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
    amount_str = _format_amount(data)
    await send_payment_receipt(user.email, tier, amount_str, data.get("id", ""))


async def _on_subscription_updated(data: dict, session: AsyncSession) -> None:
    user = await _find_user_by_paddle_customer_id(data, session)
    if not user:
        return
    tier = _tier_from_subscription(data)
    user.tier = tier
    await session.commit()
    logger.info(f"User {user.email} updated → {tier}")
    amount_str = _format_amount(data)
    await send_payment_receipt(user.email, tier, amount_str, data.get("id", ""))


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
    await send_payment_failed(user.email, data.get("id", ""))


async def _find_user_by_paddle_customer_id(data: dict, session: AsyncSession) -> User | None:
    """Look up user by paddle_customer_id, falling back to custom_data.user_id for new subscribers."""
    customer_id = data.get("customer_id")

    # Primary: existing link
    if customer_id:
        result = await session.execute(
            select(User).where(User.paddle_customer_id == customer_id)
        )
        user = result.scalar_one_or_none()
        if user:
            return user

    # Fallback: custom_data.user_id set at checkout creation (first subscription)
    user_id = (data.get("custom_data") or {}).get("user_id")
    if user_id:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            if customer_id:
                user.paddle_customer_id = customer_id  # link for future lookups
                await session.commit()
            return user

    logger.error(f"No user found for customer_id={customer_id}, custom_data={data.get('custom_data')}")
    return None


async def link_paddle_customer(user_id, customer_id: str, session: AsyncSession) -> None:
    """Call this when creating a Paddle checkout to link customer_id to user before webhook fires."""
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.paddle_customer_id = customer_id
        await session.commit()


def _format_amount(data: dict) -> str:
    """Extract a human-readable amount string from Paddle subscription data.
    Paddle v2 returns unit_price.amount as an integer string in minor units (e.g. "4900" = $49.00).
    """
    try:
        items = data.get("items", [])
        if items:
            unit_price = items[0].get("price", {}).get("unit_price", {})
            amount_str = unit_price.get("amount", "")
            currency = unit_price.get("currency_code", data.get("currency_code", "USD"))
            if amount_str:
                amount = int(amount_str) / 100
                return f"{currency} {amount:.2f}/mo"
    except (ValueError, TypeError, AttributeError, IndexError):
        pass
    return ""


def _tier_from_subscription(data: dict) -> str:
    """Map Paddle subscription to a tier using exact price IDs from config.

    Falls back to substring matching ONLY if price IDs are not configured.
    Defaults to FREE on any ambiguity — never accidentally grants PRO/AGENCY.
    """
    items = data.get("items", [])
    for item in items:
        price = item.get("price", {}) if isinstance(item.get("price"), dict) else {}
        price_id = price.get("id", "")

        # Primary path: exact price ID match (configure in settings)
        if settings.paddle_price_id_agency and price_id == settings.paddle_price_id_agency:
            return UserTier.AGENCY
        if settings.paddle_price_id_pro and price_id == settings.paddle_price_id_pro:
            return UserTier.PRO

        # Fallback: name substring match (only when price IDs not configured)
        if not settings.paddle_price_id_agency and not settings.paddle_price_id_pro:
            price_name = price.get("name", "").lower()
            if "agency" in price_name:
                return UserTier.AGENCY
            if price_name == "pro":  # exact match only, not "promotional"
                return UserTier.PRO

    logger.warning(f"Could not determine tier from subscription {data.get('id')} — defaulting FREE")
    return UserTier.FREE
