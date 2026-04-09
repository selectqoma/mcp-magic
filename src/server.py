"""
MCP Magic — Government Contracts & Grants Intelligence
FastMCP server exposing 7 tools for AI agents to search federal opportunities.
"""

import logging
import uuid as uuid_lib
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from sqlalchemy import select

from src.auth import check_search_limit, get_user_by_api_key, requires_pro
from src.db import AsyncSessionLocal
from src.models.opportunity import Opportunity
from src.services.search import _serialize, search_opportunities
from src.sync.scheduler import create_scheduler, run_initial_sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="MCP Magic — GovCon Intelligence",
    instructions=(
        "Search 75,000+ US federal contract and grant opportunities from SAM.gov, "
        "Grants.gov, and SBIR.gov. Analyze competitor awards via USASpending.gov. "
        "Authenticate by passing your API key as: api_key=<your-key> in tool calls, "
        "or set the MCP_API_KEY environment variable. Get a free key at mcpmagic.io."
    ),
)

_scheduler = None


@asynccontextmanager
async def lifespan(_):
    global _scheduler
    logger.info("Starting MCP Magic server...")
    _scheduler = create_scheduler()
    _scheduler.start()
    # Run initial sync in background
    import asyncio
    asyncio.create_task(run_initial_sync())
    yield
    if _scheduler:
        _scheduler.shutdown()


async def _resolve_user(api_key: str):
    """Resolve API key to user. Returns (user, error_message)."""
    if not api_key:
        return None, "api_key is required. Get a free key at mcpmagic.io."
    async with AsyncSessionLocal() as session:
        user = await get_user_by_api_key(api_key, session)
        if not user:
            return None, "Invalid API key. Get a free key at mcpmagic.io."
        return user, None


# ─────────────────────────────────────────────
# Tool 1: search_opportunities
# ─────────────────────────────────────────────

@mcp.tool()
async def search_opportunities_tool(
    api_key: str,
    query: str = "",
    naics_codes: list[str] | None = None,
    set_aside_type: str | None = None,
    agency: str | None = None,
    value_min: float | None = None,
    value_max: float | None = None,
    deadline_after: str | None = None,
    deadline_before: str | None = None,
    source: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Search federal contract and grant opportunities across SAM.gov, Grants.gov, and SBIR.gov.

    Args:
        api_key: Your MCP Magic API key (get one free at mcpmagic.io)
        query: Free-text semantic search (e.g. "cybersecurity zero trust DoD")
        naics_codes: Filter by NAICS codes (e.g. ["541511", "541512"])
        set_aside_type: Filter by set-aside (e.g. "SB", "8a", "HUBZone", "SDVOSB", "WOSB")
        agency: Filter by agency name (partial match, e.g. "Department of Defense")
        value_min: Minimum estimated contract value in USD
        value_max: Maximum estimated contract value in USD
        deadline_after: Only show opportunities with deadline after this date (ISO format YYYY-MM-DD)
        deadline_before: Only show opportunities with deadline before this date (ISO format YYYY-MM-DD)
        source: Filter by source ("sam", "grants", or "sbir")
        limit: Number of results (max 50)
        offset: Pagination offset

    Returns:
        dict with results (list of opportunities), total_count, has_more
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}

    async with AsyncSessionLocal() as session:
        allowed, reason = await check_search_limit(user, session)
        if not allowed:
            return {"error": reason}

        return await search_opportunities(
            session,
            query=query,
            naics_codes=naics_codes,
            set_aside_type=set_aside_type,
            agency=agency,
            value_min=value_min,
            value_max=value_max,
            deadline_after=deadline_after,
            deadline_before=deadline_before,
            source=source,
            limit=min(limit, 50),
            offset=offset,
        )


# ─────────────────────────────────────────────
# Tool 2: get_opportunity_details
# ─────────────────────────────────────────────

@mcp.tool()
async def get_opportunity_details(
    api_key: str,
    opportunity_id: str,
) -> dict:
    """Get full details of a specific opportunity including description, contacts, and attachments.

    Args:
        api_key: Your MCP Magic API key
        opportunity_id: The opportunity's UUID (from search results) or source_id (e.g. "SAM-W911NF25R0001")

    Returns:
        Full opportunity details including description, contacts, attachments list
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}

    async with AsyncSessionLocal() as session:
        # Try UUID first, then source_id
        opp = None
        try:
            uid = uuid_lib.UUID(opportunity_id)
            result = await session.execute(select(Opportunity).where(Opportunity.id == uid))
            opp = result.scalar_one_or_none()
        except ValueError:
            pass

        if not opp:
            result = await session.execute(
                select(Opportunity).where(Opportunity.source_id == opportunity_id)
            )
            opp = result.scalar_one_or_none()

        if not opp:
            return {"error": f"Opportunity '{opportunity_id}' not found."}

        data = _serialize(opp)
        data["description"] = opp.description
        data["requirements"] = opp.requirements
        return data
