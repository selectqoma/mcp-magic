"""Standalone sync worker process.

Usage:
  python worker.py            # normal worker: initial sync + scheduled syncs
  python worker.py backfill   # one-time backfill of SAM.gov historical data (1 year)
"""

import asyncio
import logging
import sys

from src.sync.scheduler import create_scheduler, run_initial_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        logger.info("Running SAM.gov backfill (1 year, 7-day chunks)...")
        from src.sync.sam import backfill_sam_opportunities
        total = await backfill_sam_opportunities(days_back=365, chunk_days=7)
        logger.info(f"Backfill complete: {total} total opportunities ingested")
        return

    logger.info("Starting sync worker...")
    await run_initial_sync()

    scheduler = create_scheduler()
    scheduler.start()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Worker stopped.")


if __name__ == "__main__":
    asyncio.run(main())
