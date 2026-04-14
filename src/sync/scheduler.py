import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings
from src.services.notifications import run_notification_matching
from src.sync.grants import sync_grants_opportunities
from src.sync.sam import enrich_sam_descriptions, sync_sam_opportunities
from src.sync.sbir import sync_sbir_opportunities
from src.sync.usaspending import backfill_usaspending_awards, sync_usaspending_awards

logger = logging.getLogger(__name__)


async def run_sync(name: str, fn):
    logger.info(f"Starting {name} sync...")
    try:
        count = await fn()
        logger.info(f"{name} sync done: {count} records")
    except Exception as e:
        logger.error(f"{name} sync failed: {e}", exc_info=True)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        run_sync, args=["SAM.gov", sync_sam_opportunities],
        trigger=IntervalTrigger(seconds=settings.sam_sync_interval),
        id="sam_sync", replace_existing=True,
    )
    scheduler.add_job(
        run_sync, args=["Grants.gov", sync_grants_opportunities],
        trigger=IntervalTrigger(seconds=settings.grants_sync_interval),
        id="grants_sync", replace_existing=True,
    )
    scheduler.add_job(
        run_sync, args=["SBIR.gov", sync_sbir_opportunities],
        trigger=IntervalTrigger(seconds=settings.sbir_sync_interval),
        id="sbir_sync", replace_existing=True,
    )
    scheduler.add_job(
        run_sync, args=["USASpending", sync_usaspending_awards],
        trigger=IntervalTrigger(seconds=settings.usaspending_sync_interval),
        id="usaspending_sync", replace_existing=True,
    )
    scheduler.add_job(
        run_sync, args=["Notifications", run_notification_matching],
        trigger=IntervalTrigger(seconds=settings.sam_sync_interval),
        id="notification_matching", replace_existing=True,
    )
    # Run description enrichment after each SAM sync to backfill URL placeholders
    # in batches of 50 (quota-friendly — stops automatically when quota exhausted)
    scheduler.add_job(
        run_sync, args=["SAM-description-enrich", enrich_sam_descriptions],
        trigger=IntervalTrigger(seconds=settings.sam_sync_interval),
        id="sam_desc_enrich", replace_existing=True,
    )

    return scheduler


async def run_initial_sync():
    """Run all syncs once at startup."""
    await run_sync("SAM.gov", sync_sam_opportunities)
    await run_sync("Grants.gov", sync_grants_opportunities)
    await run_sync("SBIR.gov", sync_sbir_opportunities)
    await run_sync("USASpending", sync_usaspending_awards)
    await run_sync("SAM-description-enrich", enrich_sam_descriptions)
    # Note: USASpending backfill (90 days) is triggered manually via /admin/backfill-awards
