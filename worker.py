"""Standalone sync worker process."""

import asyncio
import logging

from src.sync.scheduler import create_scheduler, run_initial_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def main():
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
