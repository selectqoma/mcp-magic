"""Main entrypoint: mounts FastMCP server onto FastAPI app."""

import asyncio
import logging

import uvicorn
from fastapi import FastAPI

from src.api import app as api_app
from src.server import mcp
from src.sync.scheduler import create_scheduler, run_initial_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

# Mount MCP SSE transport onto FastAPI
mcp_app = mcp.get_asgi_app()
api_app.mount("/sse", mcp_app)

_scheduler = None


async def startup():
    global _scheduler
    _scheduler = create_scheduler()
    _scheduler.start()
    asyncio.create_task(run_initial_sync())
    logger.info("MCP Magic server started")


async def shutdown():
    if _scheduler:
        _scheduler.shutdown()


api_app.add_event_handler("startup", startup)
api_app.add_event_handler("shutdown", shutdown)

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:api_app", host="0.0.0.0", port=port, reload=False)
