"""
GovCon MCP — Government Contracts & Grants Intelligence
FastMCP server exposing 7 tools for AI agents to search federal opportunities.
"""

import asyncio
import logging
import re
import uuid as uuid_lib
from datetime import UTC, datetime, timedelta

import httpx
from fastmcp import FastMCP
from sqlalchemy import and_, select

from src.auth import check_search_limit, get_user_by_api_key, requires_pro
from src.config import settings
from src.db import AsyncSessionLocal
from src.models.award import Award
from src.models.notification import Notification
from src.models.opportunity import Opportunity
from src.models.profile import CompanyProfile
from src.services.matching import match_opportunities_to_profile
from src.services.parsing import parse_solicitation_pdf
from src.services.search import _serialize, search_opportunities as _search_service
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _truncate_list(lst: list | None, max_items: int, max_item_len: int) -> list | None:
    """Cap a list to max_items, each item truncated to max_item_len chars."""
    if lst is None:
        return None
    return [str(item)[:max_item_len] for item in lst[:max_items]]


async def _resolve_attachment_filename(url: str) -> str | None:
    """Resolve a real filename for a SAM attachment URL.

    Tries (in order):
    1. Content-Disposition header from HEAD/GET response
    2. Filename extracted from the final (redirect) URL path
    """
    try:
        params = {}
        if settings.sam_gov_api_key:
            params["api_key"] = settings.sam_gov_api_key
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.head(url, params=params)
            # 1. Content-Disposition header
            cd = resp.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';\r\n]+)', cd, re.IGNORECASE)
            if m:
                return m.group(1).strip().strip('"\'')
            # 2. Extract from final URL path — only if it looks like a real filename
            # (not a UUID/hex resource ID like "3a19960569af4a638315d1ea0428d3f5.pdf")
            final_url = str(resp.url)
            path = final_url.split("?")[0].rstrip("/")
            basename = path.split("/")[-1]
            if "." in basename and len(basename) > 4:
                name_part = basename.rsplit(".", 1)[0]
                # Skip pure hex UUIDs (32+ hex chars — these are S3 resource IDs, not real names)
                if not re.match(r"^[0-9a-f]{16,}$", name_part, re.IGNORECASE):
                    return basename
    except Exception:
        pass
    return None


def _strip_html_text(html: str) -> str:
    from html.parser import HTMLParser

    class _S(HTMLParser):
        def __init__(self):
            super().__init__()
            self._parts: list[str] = []
        def handle_data(self, d):
            self._parts.append(d)
        def text(self):
            return re.sub(r"\s+", " ", " ".join(self._parts)).strip()

    s = _S()
    s.feed(html)
    return s.text()[:3000]


def _clean_description(text: str | None) -> str | None:
    """Strip HTML tags, decode entities, and normalize whitespace from descriptions."""
    if not text:
        return text
    import html
    # Remove all HTML tags
    cleaned = re.sub(r"<[^>]+>", " ", text)
    # Decode HTML entities (e.g. &#8239; → narrow no-break space → regular space)
    cleaned = html.unescape(cleaned)
    # Replace non-breaking/narrow spaces with regular spaces
    cleaned = re.sub(r"[\u00a0\u202f\u2007\u2009\u200a]", " ", cleaned)
    # Collapse whitespace
    cleaned = re.sub(r" {2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() or None


def _extract_desc_from_sam_response(data: dict) -> str:
    """Pull plain-text description from a SAM v2/v3 opportunity response dict."""
    items = data.get("opportunitiesData") or data.get("opportunities") or []
    if not items:
        return ""
    notice = items[0].get("notice") or items[0]
    desc = notice.get("description") or notice.get("synopsis", "") or ""
    if desc and not desc.startswith("http"):
        return _strip_html_text(desc)
    return ""


async def _fetch_description_for_url(url: str) -> str:
    """Fetch plain-text description for a SAM opportunity from a stored URL.

    SAM backfill records store v1 noticedesc URLs that are now deprecated (404).
    We extract the noticeid and re-fetch via v2/v3 SAM APIs.
    """
    if not settings.sam_gov_api_key:
        return ""
    key = settings.sam_gov_api_key
    # Extract noticeid from v1 URL: .../v1/noticedesc?noticeid=<id>
    m = re.search(r"noticeid=([a-zA-Z0-9]+)", url, re.IGNORECASE)
    noticeid = m.group(1) if m else None

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            if noticeid:
                # Try SAM v2 search by noticeid
                r = await client.get(
                    "https://api.sam.gov/opportunities/v2/search",
                    params={"api_key": key, "noticeid": noticeid, "limit": 1},
                )
                if r.status_code == 200:
                    text = _extract_desc_from_sam_response(r.json())
                    if text:
                        return text[:3000]
                    # If v2 search returns a URL desc, try fetching that URL too
                    items = r.json().get("opportunitiesData") or []
                    if items:
                        notice = items[0].get("notice") or items[0]
                        inner_url = notice.get("description") or ""
                        if inner_url.startswith("http"):
                            ir = await client.get(inner_url, params={"api_key": key})
                            if ir.status_code == 200 and len(ir.text) > 50:
                                return _strip_html_text(ir.text)[:3000]

                # Try SAM v2 noticedesc (different path from v1)
                for path in [
                    f"https://api.sam.gov/prod/opportunities/v2/noticedesc",
                    f"https://api.sam.gov/prod/opps/v3/opportunities/{noticeid}",
                ]:
                    r = await client.get(path, params={"api_key": key, "noticeid": noticeid})
                    if r.status_code == 200 and len(r.text) > 50:
                        ct = r.headers.get("content-type", "")
                        if "json" in ct:
                            data = r.json()
                            notice = data.get("notice") or data
                            desc = notice.get("description") or notice.get("synopsis", "") or ""
                            if desc and not desc.startswith("http"):
                                return _strip_html_text(desc)[:3000]
                        else:
                            return _strip_html_text(r.text)[:3000]

            # Fallback: try the original URL directly with auth
            r = await client.get(url, params={"api_key": key})
            if r.status_code == 429:
                logger.warning("SAM API quota exhausted for description fetch (429)")
                return ""
            if r.status_code == 200 and len(r.text) > 50:
                return _strip_html_text(r.text)[:3000]
    except Exception as e:
        logger.debug(f"SAM description fetch error: {e}")
    return ""


async def _sam_attachment_filenames(source_id: str) -> dict[int, str]:
    """Fetch real filenames for SAM attachments via the v3 resources API.
    Returns {index: filename} mapping.
    """
    if not settings.sam_gov_api_key or not source_id:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://api.sam.gov/prod/opps/v3/opportunities/{source_id}/resources",
                params={"api_key": settings.sam_gov_api_key},
            )
            if r.status_code != 200:
                logger.debug(f"SAM v3 resources {source_id}: status {r.status_code}")
                return {}
            data = r.json()
            logger.debug(f"SAM v3 resources {source_id}: keys={list(data.keys())}")
            # Walk any list value in the response — SAM API field names vary by version
            files = None
            for key in ("opportunityAttachments", "attachments", "fileInfoList", "files",
                        "resources", "links", "resourceLinks", "items"):
                val = data.get(key)
                if isinstance(val, list) and val:
                    files = val
                    break
            # Last resort: find the first list value in the response
            if files is None:
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        files = v
                        break
            if not files:
                return {}
            result = {}
            for i, f in enumerate(files):
                if not isinstance(f, dict):
                    continue
                name = (f.get("name") or f.get("filename") or f.get("resourceName")
                        or f.get("title") or f.get("fileTitle") or f.get("originalFilename"))
                if name and "." in str(name):
                    result[i] = str(name)
            return result
    except Exception as e:
        logger.debug(f"SAM v3 resources error for {source_id}: {e}")
    return {}


async def _enrich_attachments(
    attachments: list,
    source: str | None = None,
    source_id: str | None = None,
) -> list:
    """Resolve real filenames for attachments where filename is a sequential index."""
    if not attachments:
        return attachments
    has_numeric = any(str(a.get("filename", "")).isdigit() for a in attachments)
    if not has_numeric:
        return attachments

    enriched = [dict(a) for a in attachments]

    # For SAM: use the v3 resources API (one call for all files)
    if source == "sam" and source_id:
        filenames = await _sam_attachment_filenames(source_id)
        for idx, name in filenames.items():
            if idx < len(enriched) and str(enriched[idx].get("filename", "")).isdigit():
                enriched[idx]["filename"] = name
        # If v3 resolved all filenames, return; otherwise fall through to HEAD requests
        still_numeric = any(str(enriched[i].get("filename", "")).isdigit() for i in range(len(enriched)))
        if not still_numeric:
            return enriched

    # Fallback: individual HEAD/GET requests per attachment
    to_resolve = [
        (i, att) for i, att in enumerate(attachments)
        if att.get("url") and str(att.get("filename", "")).isdigit()
    ]
    if not to_resolve:
        return enriched
    results = await asyncio.gather(
        *[_resolve_attachment_filename(att["url"]) for _, att in to_resolve],
        return_exceptions=True,
    )
    for (idx, _), result in zip(to_resolve, results):
        if isinstance(result, str) and result:
            enriched[idx]["filename"] = result
        else:
            # Last resort: clean label with correct extension from URL
            url = attachments[idx].get("url", "")
            ext = url.rstrip("/").split("?")[0].rsplit(".", 1)[-1][:6].lower()
            ext = ext if ext in ("pdf", "docx", "doc", "xlsx", "pptx", "txt", "zip") else "pdf"
            enriched[idx]["filename"] = f"attachment_{idx + 1}.{ext}"
    return enriched


mcp = FastMCP(
    name="GovCon MCP — Government Contract Intelligence",
    instructions=(
        "Search 75,000+ US federal contract and grant opportunities from SAM.gov, "
        "Grants.gov, and SBIR.gov. Analyze competitor awards via USASpending.gov. "
        "Authenticate by passing your API key as: api_key=<your-key> in tool calls. "
        "Get a free key at govconmcp.com."
    ),
)


async def _resolve_user(api_key: str):
    """Resolve API key to user. Returns (user, error_message)."""
    if not api_key:
        return None, "api_key is required. Get a free key at govconmcp.com."
    if len(api_key) > 100:
        return None, "Invalid API key. Get a free key at govconmcp.com."
    async with AsyncSessionLocal() as session:
        user = await get_user_by_api_key(api_key, session)
        if not user:
            return None, "Invalid API key. Get a free key at govconmcp.com."
        return user, None


# ─────────────────────────────────────────────
# Tool 1: search_opportunities
# ─────────────────────────────────────────────

@mcp.tool()
async def search_opportunities(
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
        api_key: Your GovCon MCP API key (get one free at govconmcp.com)
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

    # Input sanitization
    query = query[:500]
    offset = max(0, min(offset, 10000))
    naics_codes = _truncate_list(naics_codes, 20, 10)
    if set_aside_type is not None:
        set_aside_type = set_aside_type[:100]
    if agency is not None:
        agency = agency[:100]
    if source is not None:
        source = source[:100]

    async with AsyncSessionLocal() as session:
        allowed, reason = await check_search_limit(user, session)
        if not allowed:
            return {"error": reason}

        return await _search_service(
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
        api_key: Your GovCon MCP API key
        opportunity_id: The opportunity's UUID (from search results) or source_id

    Returns:
        Full opportunity details including description, contacts, attachments list
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}

    # Input sanitization
    opportunity_id = opportunity_id[:255]

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

        description = opp.description or ""
        sam_gov_url = None

        if opp.source == "sam":
            # Always construct public URL from source_id (noticeId).
            # The SAM API's uiLink uses /workspace/contract/opp/ which requires login;
            # the public URL is /opp/{noticeId}/view and works without authentication.
            if opp.source_id:
                sam_gov_url = f"https://sam.gov/opp/{opp.source_id}/view"
            else:
                # Fallback: extract noticeid from description URL
                m = re.search(r"noticeid=([a-zA-Z0-9]+)", description, re.IGNORECASE)
                if m:
                    sam_gov_url = f"https://sam.gov/opp/{m.group(1)}/view"

            # Lazy-fetch description if stored as a URL placeholder or null.
            # Use source_id (noticeId) directly — more reliable than extracting from URL.
            if description.startswith("http") or not description:
                fetch_url = description if description.startswith("http") else (
                    f"https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid={opp.source_id}"
                    if opp.source_id else ""
                )
                fetched = await _fetch_description_for_url(fetch_url) if fetch_url else ""
                if fetched:
                    description = fetched
                    opp.description = fetched
                    await session.commit()
                elif description.startswith("http"):
                    # URL placeholder but can't fetch — hide raw URL from agents
                    description = None

        data = _serialize(opp)
        data["description"] = _clean_description(description)
        data["sam_gov_url"] = sam_gov_url
        data["requirements"] = opp.requirements
        data["attachments"] = await _enrich_attachments(opp.attachments or [], opp.source, opp.source_id)
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
        api_key: Your GovCon MCP API key (Pro tier required)
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

        # SAM attachment URLs require ?api_key= to download (they redirect to S3 pre-signed URLs).
        # Append the API key so the download doesn't return a 403 HTML error page.
        if (
            opp.source == "sam"
            and "sam.gov" in pdf_url
            and settings.sam_gov_api_key
            and "api_key" not in pdf_url
        ):
            sep = "&" if "?" in pdf_url else "?"
            pdf_url = f"{pdf_url}{sep}api_key={settings.sam_gov_api_key}"

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
    source: str | None = None,
    max_results: int = 10,
    min_score: float = 0.25,
) -> dict:
    """Score and rank current opportunities against your company profile.

    Scoring: keyword overlap (45%), NAICS match (25%), semantic similarity (20%),
    set-aside eligibility (10%). Low-relevance results are filtered by a hard gate
    regardless of set-aside status.

    Tip: use source="sam" to restrict results to federal contracts only (excludes
    grants and SBIR awards that may appear when keyword overlap is incidental).

    Args:
        api_key: Your GovCon MCP API key (Pro tier required)
        profile_id: Saved profile UUID, OR provide inline capabilities/naics/certs
        capabilities: e.g. ["penetration testing", "cloud migration", "FedRAMP"]
        naics_codes: e.g. ["541511", "541512"]
        certifications: e.g. ["8a", "SDVOSB", "HUBZone", "WOSB", "SB"]
        past_agencies: e.g. ["Department of Defense", "DHS"]
        source: Filter by source — "sam" (contracts only), "grants", "sbir", or omit for all
        max_results: Maximum matches to return (default 10, max 25)
        min_score: Minimum match score 0-1 (default 0.25)

    Returns:
        List of scored matches with score breakdown and reasoning per match
    """
    user, err = await _resolve_user(api_key)
    if err:
        return {"error": err}
    ok, reason = requires_pro(user)
    if not ok:
        return {"error": reason}

    # Input sanitization
    capabilities = _truncate_list(capabilities, 50, 200)
    naics_codes = _truncate_list(naics_codes, 20, 10)
    certifications = _truncate_list(certifications, 20, 50)
    past_agencies = _truncate_list(past_agencies, 20, 200)
    min_score = max(0.0, min(1.0, min_score))

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
            source=source,
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
        api_key: Your GovCon MCP API key (Pro tier required)
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

    # Input sanitization
    if company_name is not None:
        company_name = company_name[:200]
    if uei is not None:
        uei = uei[:200]
    if naics_code is not None:
        naics_code = naics_code[:100]
    if agency is not None:
        agency = agency[:100]
    years = max(1, min(10, years))

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
            select(Award)
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
                    "award_id": a.award_id,
                    "title": a.title or (f"{a.award_id} — {a.agency}" if a.award_id else None),
                    "agency": a.agency,
                    "sub_agency": a.sub_agency,
                    "value": float(a.award_amount or 0),
                    "date": a.award_date.isoformat() if a.award_date else None,
                    "naics": a.naics_code,
                    "place_of_performance": a.place_of_performance,
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
    keyword: str | None = None,
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

    Use this to answer: "How much does DHS spend on IT services?"
    or "Who are the top contractors for NAICS 541511 at DoD?"

    Best filters: agency (partial name match), naics_code (exact), recipient_name (partial).
    keyword searches across title, agency, sub-agency, and recipient name — use full agency names
    (e.g. "Department of Defense") rather than topic words (e.g. "cybersecurity") for best results.

    Args:
        api_key: Your GovCon MCP API key
        keyword: Search across award title, agency, sub-agency, and recipient name.
            Use full agency names for best results: "Department of Defense", "NASA", "DHS".
            Topic keywords like "cybersecurity" won't match unless in the agency/recipient name.
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

    # Input sanitization
    if keyword is not None:
        keyword = keyword[:200]
    if agency is not None:
        agency = agency[:200]
    if recipient_name is not None:
        recipient_name = recipient_name[:200]
    if naics_code is not None:
        naics_code = naics_code[:200]

    from sqlalchemy import or_ as _or
    filters = []
    if keyword:
        # USASpending award descriptions are often null; search title, agency, and sub_agency
        filters.append(
            _or(
                Award.title.ilike(f"%{keyword}%"),
                Award.agency.ilike(f"%{keyword}%"),
                Award.sub_agency.ilike(f"%{keyword}%"),
                Award.recipient_name.ilike(f"%{keyword}%"),
            )
        )
    if agency:
        # Common acronym → full name expansion
        _ACRONYMS = {
            "nasa": "National Aeronautics and Space Administration",
            "dod": "Department of Defense", "dhs": "Department of Homeland Security",
            "doe": "Department of Energy", "hhs": "Department of Health and Human Services",
            "doj": "Department of Justice", "dot": "Department of Transportation",
            "usda": "Department of Agriculture", "va": "Department of Veterans Affairs",
            "epa": "Environmental Protection Agency", "gsa": "General Services Administration",
            "sba": "Small Business Administration", "nih": "National Institutes of Health",
            "nsf": "National Science Foundation", "darpa": "Defense Advanced Research Projects Agency",
            "dla": "Defense Logistics Agency", "fema": "Federal Emergency Management Agency",
            "tsa": "Transportation Security Administration", "cia": "Central Intelligence Agency",
        }
        # Stop words that match too many agencies
        _STOP = {"department", "national", "administration", "services", "agency",
                 "federal", "united", "states", "government", "bureau", "office",
                 "the", "and", "of", "for"}
        expanded = _ACRONYMS.get(agency.lower().strip(), agency)
        # AND-match all significant words — prevents "Department" from matching every agency
        words = [w for w in expanded.split() if len(w) >= 3 and w.lower() not in _STOP]
        if words:
            filters.append(
                and_(*(
                    _or(Award.agency.ilike(f"%{w}%"), Award.sub_agency.ilike(f"%{w}%"))
                    for w in words
                ))
            )
        else:
            filters.append(
                _or(Award.agency.ilike(f"%{agency}%"), Award.sub_agency.ilike(f"%{agency}%"))
            )
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
            select(Award)
            .where(and_(*filters) if filters else Award.award_id.is_not(None))
            .order_by(Award.award_date.desc().nulls_last())
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

        note = None
        if keyword and not awards:
            note = (
                "No awards matched this keyword. USASpending award titles are often brief "
                "contract IDs (e.g. 'W52P1J24A0001'). For topic searches, try naics_code= "
                "or agency= filters instead. Example: agency='Cybersecurity' or naics_code='541511'."
            )

        return {
            "awards": [
                {
                    "award_id": a.award_id,
                    "recipient": a.recipient_name,
                    "agency": a.agency,
                    "title": a.title or (f"{a.award_id} — {a.agency}" if a.award_id else None),
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
            **({"note": note} if note else {}),
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
        api_key: Your GovCon MCP API key (Pro tier required)
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

        # Verify the profile belongs to this user before returning notifications
        profile_check = await session.execute(
            select(CompanyProfile).where(
                CompanyProfile.id == profile_uid,
                CompanyProfile.user_id == user.id,
            )
        )
        if not profile_check.scalar_one_or_none():
            return {"error": "Profile not found or access denied."}

        filters = [Notification.profile_id == profile_uid]
        if unread_only:
            filters.append(Notification.read.is_(False))

        stmt = (
            select(Notification, Opportunity)
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
        api_key: Your GovCon MCP API key (Pro tier required)
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

    # Input sanitization
    name = name[:200]
    if description is not None:
        description = description[:2000]
    capabilities = _truncate_list(capabilities, 50, 200)
    naics_codes = _truncate_list(naics_codes, 20, 10)
    certifications = _truncate_list(certifications, 20, 50)
    past_agencies = _truncate_list(past_agencies, 50, 200)

    from src.models.user import TIER_LIMITS, UserTier
    # requires_pro already passed above, so user is either PRO/AGENCY or in trial.
    # During trial, grant Pro-level limits; otherwise use the actual tier.
    from src.auth import requires_pro as _rp
    from datetime import UTC, datetime as _dt
    _now = _dt.now(UTC)
    _trial_end = user.trial_ends_at
    if _trial_end and _trial_end.tzinfo is None:
        _trial_end = _trial_end.replace(tzinfo=UTC)
    _in_trial = _trial_end is not None and _trial_end > _now and user.tier == UserTier.FREE
    _effective_tier = UserTier.PRO if _in_trial else user.tier
    max_profiles = TIER_LIMITS.get(_effective_tier, {}).get("profiles", 3)

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
        api_key: Your GovCon MCP API key

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
