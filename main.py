"""Main entrypoint: mounts FastMCP server onto FastAPI app via SSE transport."""

import asyncio
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import sqlalchemy as sa

from src.sync.scheduler import create_scheduler, run_initial_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


async def ensure_tables():
    """Guarantee all tables exist regardless of alembic state.

    create_all is idempotent — it skips tables that already exist. This is
    the safety net when alembic marks a migration as done but DDL was never
    committed (async SQLAlchemy edge-case we've hit repeatedly in Railway).
    """
    import src.models  # noqa: F401 — registers all ORM models on Base.metadata
    from src.db import Base, engine

    try:
        async with engine.begin() as conn:
            # pgvector extension — needed for Vector(1536) column on opportunities
            await conn.execute(sa.text("""
                DO $$ BEGIN
                    CREATE EXTENSION IF NOT EXISTS vector;
                EXCEPTION WHEN OTHERS THEN NULL;
                END $$;
            """))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("ensure_tables: all tables present.")
    except Exception as e:
        logger.error(f"ensure_tables failed: {e}", exc_info=True)

_scheduler = None
_initial_sync_done = False

# Build the MCP ASGI app once at module init so its lifespan can be referenced
# before create_app() is called.
def _build_mcp_asgi():
    from src.server import mcp
    try:
        return mcp.sse_app()
    except AttributeError:
        return mcp.http_app(path="/")

_mcp_asgi = _build_mcp_asgi()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler, _initial_sync_done

    async with AsyncExitStack() as stack:
        # Propagate FastMCP's own lifespan (initializes session manager / task groups)
        if hasattr(_mcp_asgi, "router") and callable(
            getattr(_mcp_asgi.router, "lifespan_context", None)
        ):
            try:
                await stack.enter_async_context(_mcp_asgi.router.lifespan_context(app))
                logger.info("FastMCP lifespan started.")
            except Exception as e:
                logger.warning(f"Could not enter FastMCP lifespan: {e}")

        logger.info("Starting MCP Magic server...")
        await ensure_tables()
        _scheduler = create_scheduler()
        _scheduler.start()

        if not _initial_sync_done:
            _initial_sync_done = True
            asyncio.create_task(_safe_initial_sync())

        yield

        if _scheduler:
            _scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down.")


async def _safe_initial_sync():
    try:
        await run_initial_sync()
    except Exception as e:
        logger.error(f"Initial sync failed: {e}", exc_info=True)


def create_app() -> FastAPI:
    from src.api import app as api_app

    main_app = FastAPI(title="MCP Magic", lifespan=lifespan, docs_url=None, redoc_url=None)

    main_app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "https://govconmcp.com",
            "https://www.govconmcp.com",
            "http://localhost:3000",
        ],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    # REST routes (register, webhooks, landing page)
    main_app.include_router(api_app.router)

    # FastMCP ASGI — SSE endpoint at /mcp/sse, messages at /mcp/messages/
    main_app.mount("/mcp", _mcp_asgi)
    logger.info("FastMCP mounted at /mcp")

    return main_app


app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
