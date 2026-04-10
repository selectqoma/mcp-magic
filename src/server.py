"""
MCP Magic — Government Contracts & Grants Intelligence
FastMCP server exposing 7 tools for AI agents to search federal opportunities.
"""

import logging
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

from fastmcp import FastMCP
from sqlalchemy import and_, select
from sqlalchemy import select as sa_select

from src.auth import check_search_limit, get_user_by_api_key, requires_pro
from src.db import AsyncSessionLocal
from src.models.award import Award
from src.models.notification import Notification
from src.models.opportunity import Opportunity
from src.models.profile import CompanyProfile
from src.services.matching import match_opportunities_to_profile
from src.services.parsing import parse_solicitation_pdf
from src.services.search import _serialize, search_opportunities
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="MCP Magic — GovCon Intelligence",
    instructions=(
        "Search 75,000+ US federal contract and grant opportunities from SAM.gov, "
        "Grants.gov, and SBIR.gov. Analyze competitor awards via USASpending.gov. "
        "Authenticate by passing your API key as: api_key=<your-key> in tool calls. "
        "Get a free key at mcpmagic.io."
    ),
)


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
        deadline_after: Only show opportunities with deadline after this date (ISO YYYY-MM-DD)
        deadline_before: Only show opportunities with deadline before this date (ISO YYYY-MM-DD)
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
        opportunity_id: The opportunity's UUID (from search results) or source_id

    Returns:
        Full opportunity details including description, contacts, attachments list
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}

    async with AsyncSessionLocal() as session:
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


# ─────────────────────────────────────────────
# Tool 3: get_solicitation_requirements (Pro)
# ─────────────────────────────────────────────

@mcp.tool()
async def get_solicitation_requirements(
    api_key: str,
    opportunity_id: str,
) -> dict:
    """Parse PDF attachments and extract structured requirements from a solicitation.

    Extracts: technical requirements, evaluation criteria, key dates, page limits,
    eligible entity types, clearance requirements, and period of performance.

    Args:
        api_key: Your MCP Magic API key (Pro tier required)
        opportunity_id: UUID or source_id of the opportunity

    Returns:
        Structured requirements dict with parsing_confidence score (0-1)
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}
    ok, reason = requires_pro(user)
    if not ok:
        return {"error": reason}

    async with AsyncSessionLocal() as session:
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

        # Return cached result if available
        if opp.requirements:
            return opp.requirements

        attachments = opp.attachments or []
        pdf_url = next(
            (a["url"] for a in attachments if a.get("url") and a["url"].lower().endswith(".pdf")),
            next((a["url"] for a in attachments if a.get("url")), None),
        )

        if not pdf_url:
            return {"error": "No downloadable attachments found for this opportunity."}

        requirements = await parse_solicitation_pdf(pdf_url)
        opp.requirements = requirements
        await session.commit()
        return requirements


# ─────────────────────────────────────────────
# Tool 4: match_to_profile (Pro)
# ─────────────────────────────────────────────

@mcp.tool()
async def match_to_profile(
    api_key: str,
    profile_id: str | None = None,
    capabilities: list[str] | None = None,
    naics_codes: list[str] | None = None,
    certifications: list[str] | None = None,
    past_agencies: list[str] | None = None,
    max_results: int = 10,
    min_score: float = 0.3,
) -> dict:
    """Score and rank current opportunities against your company profile.

    Scoring: keyword overlap (40%), NAICS match (20%), set-aside eligibility (20%),
    semantic similarity (20%).

    Args:
        api_key: Your MCP Magic API key (Pro tier required)
        profile_id: Saved profile UUID, OR provide inline capabilities/naics/certs
        capabilities: e.g. ["penetration testing", "cloud migration", "FedRAMP"]
        naics_codes: e.g. ["541511", "541512"]
        certifications: e.g. ["8a", "SDVOSB", "HUBZone", "WOSB", "SB"]
        past_agencies: e.g. ["Department of Defense", "DHS"]
        max_results: Maximum matches to return (default 10, max 25)
        min_score: Minimum match score 0-1 (default 0.3)

    Returns:
        List of scored matches with score breakdown and reasoning per match
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}
    ok, reason = requires_pro(user)
    if not ok:
        return {"error": reason}

    async with AsyncSessionLocal() as session:
        profile = None
        if profile_id:
            try:
                uid = uuid_lib.UUID(profile_id)
                result = await session.execute(
                    select(CompanyProfile).where(
                        CompanyProfile.id == uid,
                        CompanyProfile.user_id == user.id,
                    )
                )
                profile = result.scalar_one_or_none()
            except ValueError:
                pass

        return await match_opportunities_to_profile(
            session,
            profile=profile,
            capabilities=capabilities,
            naics_codes=naics_codes,
            certifications=certifications,
            past_agencies=past_agencies,
            max_results=min(max_results, 25),
            min_score=min_score,
        )


# ─────────────────────────────────────────────
# Tool 5: track_competitors (Pro)
# ─────────────────────────────────────────────

@mcp.tool()
async def track_competitors(
    api_key: str,
    company_name: str | None = None,
    uei: str | None = None,
    naics_code: str | None = None,
    agency: str | None = None,
    years: int = 3,
) -> dict:
    """Look up a company's federal contract award history from USASpending data.

    Args:
        api_key: Your MCP Magic API key (Pro tier required)
        company_name: Company name (partial match)
        uei: Unique Entity Identifier (exact match, preferred over name)
        naics_code: Filter awards by NAICS code
        agency: Filter awards by agency name
        years: How many years back to look (default 3)

    Returns:
        Summary with total awards, top agencies, top NAICS, and recent awards list
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}
    ok, reason = requires_pro(user)
    if not ok:
        return {"error": reason}

    if not company_name and not uei:
        return {"error": "Provide company_name or uei."}

    cutoff = datetime.now(UTC) - timedelta(days=years * 365)

    async with AsyncSessionLocal() as session:
        filters = [Award.award_date >= cutoff]
        if uei:
            filters.append(Award.recipient_uei == uei)
        elif company_name:
            filters.append(Award.recipient_name.ilike(f"%{company_name}%"))
        if naics_code:
            filters.append(Award.naics_code == naics_code)
        if agency:
            filters.append(Award.agency.ilike(f"%{agency}%"))

        stmt = (
            sa_select(Award)
            .where(and_(*filters))
            .order_by(Award.award_date.desc())
            .limit(200)
        )
        result = await session.execute(stmt)
        awards = result.scalars().all()

        if not awards:
            return {
                "message": f"No awards found for '{company_name or uei}' in the last {years} years.",
                "summary": None,
                "recent_awards": [],
            }

        total_value = sum(float(a.award_amount or 0) for a in awards)
        avg_value = total_value / len(awards)

        agency_counts: dict[str, dict] = {}
        for a in awards:
            key = a.agency or "Unknown"
            if key not in agency_counts:
                agency_counts[key] = {"count": 0, "value": 0}
            agency_counts[key]["count"] += 1
            agency_counts[key]["value"] += float(a.award_amount or 0)

        top_agencies = sorted(agency_counts.items(), key=lambda x: x[1]["value"], reverse=True)[:5]

        naics_counts: dict[str, int] = {}
        for a in awards:
            if a.naics_code:
                naics_counts[a.naics_code] = naics_counts.get(a.naics_code, 0) + 1
        top_naics = sorted(naics_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "company": {
                "name": awards[0].recipient_name,
                "uei": awards[0].recipient_uei,
                "cage": awards[0].recipient_cage,
            },
            "summary": {
                "total_awards": len(awards),
                "total_value": round(total_value, 2),
                "avg_award_value": round(avg_value, 2),
                "years_analyzed": years,
                "top_agencies": [
                    {"name": k, "count": v["count"], "total_value": round(v["value"], 2)}
                    for k, v in top_agencies
                ],
                "top_naics": [{"code": k, "count": v} for k, v in top_naics],
            },
            "recent_awards": [
                {
                    "title": a.title,
                    "agency": a.agency,
                    "value": float(a.award_amount or 0),
                    "date": a.award_date.isoformat() if a.award_date else None,
                    "naics": a.naics_code,
                }
                for a in awards[:20]
            ],
        }


# ─────────────────────────────────────────────
# Tool 6: search_past_awards
# ─────────────────────────────────────────────

@mcp.tool()
async def search_past_awards(
    api_key: str,
    agency: str | None = None,
    naics_code: str | None = None,
    recipient_name: str | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    awarded_after: str | None = None,
    awarded_before: str | None = None,
    limit: int = 20,
) -> dict:
    """Search historical federal contract awards for market intelligence.

    Use this to answer: "How much does DoD spend on cybersecurity?"
    or "Who are the top contractors for NAICS 541511 at DHS?"

    Args:
        api_key: Your MCP Magic API key
        agency: Filter by awarding agency (partial match)
        naics_code: Filter by NAICS code (exact)
        recipient_name: Filter by recipient company name (partial match)
        min_value: Minimum award amount in USD
        max_value: Maximum award amount in USD
        awarded_after: Only awards after this date (YYYY-MM-DD)
        awarded_before: Only awards before this date (YYYY-MM-DD)
        limit: Number of results (max 50)

    Returns:
        List of awards + aggregate statistics (total value, avg, top recipients)
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}

    filters = []
    if agency:
        filters.append(Award.agency.ilike(f"%{agency}%"))
    if naics_code:
        filters.append(Award.naics_code == naics_code)
    if recipient_name:
        filters.append(Award.recipient_name.ilike(f"%{recipient_name}%"))
    if min_value is not None:
        filters.append(Award.award_amount >= min_value)
    if max_value is not None:
        filters.append(Award.award_amount <= max_value)
    if awarded_after:
        try:
            filters.append(Award.award_date >= datetime.fromisoformat(awarded_after).replace(tzinfo=UTC))
        except ValueError:
            pass
    if awarded_before:
        try:
            filters.append(Award.award_date <= datetime.fromisoformat(awarded_before).replace(tzinfo=UTC))
        except ValueError:
            pass

    async with AsyncSessionLocal() as session:
        stmt = (
            sa_select(Award)
            .where(and_(*filters) if filters else Award.award_id.is_not(None))
            .order_by(Award.award_amount.desc().nulls_last())
            .limit(min(limit, 50))
        )
        result = await session.execute(stmt)
        awards = result.scalars().all()

        total_value = sum(float(a.award_amount or 0) for a in awards)
        avg_value = total_value / len(awards) if awards else 0

        recipient_totals: dict[str, float] = {}
        for a in awards:
            key = a.recipient_name or "Unknown"
            recipient_totals[key] = recipient_totals.get(key, 0) + float(a.award_amount or 0)

        top_recipients = sorted(recipient_totals.items(), key=lambda x: x[1], reverse=True)[:10]

        return {
            "awards": [
                {
                    "recipient": a.recipient_name,
                    "agency": a.agency,
                    "title": a.title,
                    "value": float(a.award_amount or 0),
                    "date": a.award_date.isoformat() if a.award_date else None,
                    "naics": a.naics_code,
                    "place_of_performance": a.place_of_performance,
                }
                for a in awards
            ],
            "aggregations": {
                "total_value": round(total_value, 2),
                "avg_value": round(avg_value, 2),
                "count": len(awards),
                "top_recipients": [
                    {"name": k, "total": round(v, 2)} for k, v in top_recipients
                ],
            },
        }


# ─────────────────────────────────────────────
# Tool 7: get_notifications (Pro)
# ─────────────────────────────────────────────

@mcp.tool()
async def get_notifications(
    api_key: str,
    profile_id: str,
    unread_only: bool = True,
    limit: int = 20,
) -> dict:
    """Get new opportunity matches for a saved company profile.

    New matches are generated automatically every time opportunities are synced.
    Call this at the start of your session to catch up on what's new.

    Args:
        api_key: Your MCP Magic API key (Pro tier required)
        profile_id: Your saved company profile UUID
        unread_only: Only return unread notifications (default True)
        limit: Max notifications to return (default 20)

    Returns:
        List of new opportunity matches with match scores
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}
    ok, reason = requires_pro(user)
    if not ok:
        return {"error": reason}

    async with AsyncSessionLocal() as session:
        try:
            profile_uid = uuid_lib.UUID(profile_id)
        except ValueError:
            return {"error": "Invalid profile_id format."}

        filters = [Notification.profile_id == profile_uid]
        if unread_only:
            filters.append(Notification.read.is_(False))

        stmt = (
            sa_select(Notification, Opportunity)
            .join(Opportunity, Notification.opportunity_id == Opportunity.id)
            .where(and_(*filters))
            .order_by(Notification.matched_at.desc())
            .limit(min(limit, 50))
        )
        result = await session.execute(stmt)
        rows = result.all()

        for notif, _ in rows:
            notif.read = True
        await session.commit()

        return {
            "notifications": [
                {
                    "id": str(notif.id),
                    "opportunity_id": str(opp.id),
                    "title": opp.title,
                    "agency": opp.agency,
                    "deadline": opp.response_deadline.isoformat() if opp.response_deadline else None,
                    "match_score": notif.match_score,
                    "matched_at": notif.matched_at.isoformat(),
                    "source": opp.source,
                }
                for notif, opp in rows
            ],
            "count": len(rows),
        }


# ─────────────────────────────────────────────
# Tool 8: save_profile (Pro)
# ─────────────────────────────────────────────

@mcp.tool()
async def save_profile(
    api_key: str,
    name: str,
    capabilities: list[str] | None = None,
    naics_codes: list[str] | None = None,
    certifications: list[str] | None = None,
    past_agencies: list[str] | None = None,
    description: str | None = None,
    profile_id: str | None = None,
) -> dict:
    """Create or update a company profile for opportunity matching and notifications.

    Saved profiles are used by match_to_profile and get_notifications.
    You can have up to 3 profiles on Pro, 20 on Agency.

    Args:
        api_key: Your MCP Magic API key (Pro tier required)
        name: Company name (e.g. "Acme Cyber LLC")
        capabilities: List of capabilities (e.g. ["penetration testing", "cloud migration", "FedRAMP"])
        naics_codes: Your applicable NAICS codes (e.g. ["541511", "541512", "541519"])
        certifications: Your certifications (e.g. ["SDVOSB", "8a", "HUBZone", "WOSB", "SB"])
        past_agencies: Agencies you have past performance with (e.g. ["DoD", "DHS", "VA"])
        description: Free-text company description for semantic matching
        profile_id: UUID of existing profile to update (omit to create new)

    Returns:
        dict with profile_id, name, and confirmation message
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}
    ok, reason = requires_pro(user)
    if not ok:
        return {"error": reason}

    from src.models.user import TIER_LIMITS, UserTier
    max_profiles = TIER_LIMITS.get(user.tier, {}).get("profiles", 3)
    if max_profiles == 0:
        max_profiles = 3  # trial users get pro limits

    async with AsyncSessionLocal() as session:
        if profile_id:
            # Update existing
            try:
                uid = uuid_lib.UUID(profile_id)
            except ValueError:
                return {"error": "Invalid profile_id format."}
            result = await session.execute(
                select(CompanyProfile).where(
                    CompanyProfile.id == uid, CompanyProfile.user_id == user.id
                )
            )
            profile = result.scalar_one_or_none()
            if not profile:
                return {"error": "Profile not found or does not belong to your account."}
        else:
            # Check profile limit
            from sqlalchemy import func as sqlfunc
            count_result = await session.execute(
                select(sqlfunc.count()).select_from(CompanyProfile).where(
                    CompanyProfile.user_id == user.id
                )
            )
            count = count_result.scalar() or 0
            if count >= max_profiles:
                return {
                    "error": f"Profile limit reached ({max_profiles} on your plan). "
                    "Upgrade to Agency for 20 profiles."
                }
            profile = CompanyProfile(user_id=user.id, name=name)
            session.add(profile)

        profile.name = name
        if capabilities is not None:
            profile.capabilities = capabilities
        if naics_codes is not None:
            profile.naics_codes = naics_codes
        if certifications is not None:
            profile.certifications = certifications
        if past_agencies is not None:
            profile.past_agencies = past_agencies
        if description is not None:
            profile.description = description

        await session.commit()
        await session.refresh(profile)

        return {
            "profile_id": str(profile.id),
            "name": profile.name,
            "capabilities": profile.capabilities,
            "naics_codes": profile.naics_codes,
            "certifications": profile.certifications,
            "past_agencies": profile.past_agencies,
            "message": (
                f"Profile '{profile.name}' saved. Use profile_id={profile.id} "
                "with match_to_profile and get_notifications."
            ),
        }


# ─────────────────────────────────────────────
# Tool 9: list_profiles
# ─────────────────────────────────────────────

@mcp.tool()
async def list_profiles(api_key: str) -> dict:
    """List your saved company profiles.

    Args:
        api_key: Your MCP Magic API key

    Returns:
        List of your saved profiles with their IDs
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CompanyProfile).where(CompanyProfile.user_id == user.id)
        )
        profiles = result.scalars().all()

        return {
            "profiles": [
                {
                    "profile_id": str(p.id),
                    "name": p.name,
                    "capabilities": p.capabilities,
                    "naics_codes": p.naics_codes,
                    "certifications": p.certifications,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                for p in profiles
            ],
            "count": len(profiles),
        }
