"""Main entrypoint: mounts FastMCP server onto FastAPI app via SSE transport."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.sync.scheduler import create_scheduler, run_initial_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)

_scheduler = None
_initial_sync_done = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler, _initial_sync_done
    logger.info("Starting MCP Magic server...")
    _scheduler = create_scheduler()
    _scheduler.start()
    # Only run initial sync if not recently done (guard against restart spam)
    if not _initial_sync_done:
        _initial_sync_done = True
        asyncio.create_task(_safe_initial_sync())
    yield
    if _scheduler:
        _scheduler.shutdown()
        logger.info("Scheduler shut down.")


async def _safe_initial_sync():
    try:
        await run_initial_sync()
    except Exception as e:
        logger.error(f"Initial sync failed: {e}", exc_info=True)


# Build the unified app with a single lifespan
def create_app() -> FastAPI:
    from src.api import app as api_app
    from src.server import mcp

    # Re-create api_app with our lifespan (api_app was created without one)
    main_app = FastAPI(title="MCP Magic", lifespan=lifespan, docs_url=None, redoc_url=None)

    # CORS for the landing page registration form
    main_app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://mcpmagic.io", "http://localhost:3000"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    # Mount REST routes from api_app
    main_app.include_router(api_app.router)

    # Mount FastMCP SSE app — correct API for FastMCP 2.x
    # mcp.sse_app() returns an ASGI app that serves at /sse
    try:
        mcp_asgi = mcp.sse_app()
        main_app.mount("/mcp", mcp_asgi)
        logger.info("FastMCP mounted at /mcp (SSE endpoint: /mcp/sse)")
    except AttributeError:
        # FastMCP 2.x fallback — try http_app (streamable HTTP transport)
        try:
            mcp_asgi = mcp.http_app(path="/")
            main_app.mount("/mcp", mcp_asgi)
            logger.info("FastMCP mounted at /mcp (HTTP transport)")
        except Exception as e:
            logger.error(f"Could not mount FastMCP ASGI app: {e}. Check FastMCP version.")

    return main_app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
