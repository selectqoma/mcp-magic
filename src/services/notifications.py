"""Match new opportunities against saved profiles and create notifications."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import AsyncSessionLocal
from src.models.notification import Notification
from src.models.opportunity import Opportunity
from src.models.profile import CompanyProfile
from src.services.matching import _score_opportunity

logger = logging.getLogger(__name__)


async def run_notification_matching():
    """Check recently synced opportunities against all saved profiles."""
    async with AsyncSessionLocal() as session:
        # Get opportunities synced in the last sync window (last 30 min)
        cutoff = datetime.now(UTC) - timedelta(minutes=30)
        opps_result = await session.execute(
            select(Opportunity).where(Opportunity.created_at >= cutoff)
        )
        new_opps = opps_result.scalars().all()

        if not new_opps:
            return

        # Get all saved profiles
        profiles_result = await session.execute(select(CompanyProfile))
        profiles = profiles_result.scalars().all()

        if not profiles:
            return

        notifications_created = 0
        for profile in profiles:
            for opp in new_opps:
                score, _ = _score_opportunity(
                    opp,
                    caps=list(profile.capabilities or []),
                    naics=list(profile.naics_codes or []),
                    certs=list(profile.certifications or []),
                    profile_embedding=None,  # skip embedding for batch perf
                )
                if score >= 0.5:  # higher threshold for auto-notifications
                    notif = Notification(
                        profile_id=profile.id,
                        opportunity_id=opp.id,
                        match_score=round(score, 3),
                    )
                    session.add(notif)
                    notifications_created += 1

        if notifications_created:
            await session.commit()
            logger.info(f"Created {notifications_created} new notifications")
