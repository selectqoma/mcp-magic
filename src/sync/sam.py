"""SAM.gov opportunity sync.

Uses the beta.SAM.gov API v2:
  GET https://api.sam.gov/opportunities/v2/search
  Docs: https://open.gsa.gov/api/get-opportunities-public-api/

Register for an API key at: https://sam.gov/profile/details
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from src.config import settings
from src.db import AsyncSessionLocal
from src.embeddings import embed_batch
from src.models.opportunity import Opportunity

logger = logging.getLogger(__name__)

SAM_BASE = "https://api.sam.gov/opportunities/v2/search"
PAGE_SIZE = 100


def _parse_opportunity(item: dict) -> dict:
    """Normalize a SAM.gov API response item into our schema."""
    notice = item.get("notice") or item  # handle both wrapped and flat responses; "notice": null → fall back to item
    return {
        "source": "sam",
        "source_id": notice.get("noticeId") or notice.get("solicitationNumber", ""),
        "title": notice.get("title", ""),
        "description": notice.get("description") or notice.get("synopsis", ""),
        "agency": notice.get("fullParentPathName") or notice.get("organizationName", ""),
        "naics_codes": _parse_naics(notice),
        "set_aside_type": notice.get("typeOfSetAside") or notice.get("setAside"),
        "posted_date": _parse_date(notice.get("postedDate")),
        "response_deadline": _parse_date(
            notice.get("responseDeadLine") or notice.get("archiveDate")
        ),
        "estimated_value_low": _parse_float((notice.get("award") or {}).get("amount")),
        "estimated_value_high": None,
        "place_of_performance": _format_place(notice.get("placeOfPerformance")),
        "contact_info": notice.get("pointOfContact"),
        "attachments": _parse_attachments(notice.get("resourceLinks") or []),
        "raw_data": item,
    }


def _parse_naics(notice: dict) -> list[str]:
    """Extract NAICS codes; SAM returns either a single string or an array."""
    codes = notice.get("naicsCodes") or []
    if isinstance(codes, list):
        result = [str(c) for c in codes if c]
    else:
        result = [str(codes)] if codes else []
    # Also include the single naicsCode field if present and not already there
    single = notice.get("naicsCode")
    if single and str(single) not in result:
        result.append(str(single))
    return result


def _parse_date(val: str | None) -> datetime | None:
    if not val:
        return None
    # Try each format against the full value, not a truncated format string
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(val[:len(fmt)], fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError:
            continue
    return None


def _parse_float(val) -> float | None:
    try:
        return float(str(val).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _format_place(place: dict | None) -> str | None:
    if not place:
        return None
    parts = [
        place.get("city", {}).get("name", "") if isinstance(place.get("city"), dict) else "",
        place.get("state", {}).get("code", "") if isinstance(place.get("state"), dict) else "",
    ]
    return ", ".join(p for p in parts if p) or None


def _parse_attachments(links: list) -> list:
    return [{"filename": str(i), "url": link, "parsed": False} for i, link in enumerate(links)]


async def sync_sam_opportunities() -> int:
    """Fetch recent SAM.gov opportunities and upsert into DB. Returns count ingested."""
    if not settings.sam_gov_api_key:
        logger.warning("SAM_GOV_API_KEY not set, skipping sync")
        return 0

    # Fetch opportunities posted in the last 2 days to stay current
    posted_from = (datetime.now(UTC) - timedelta(days=2)).strftime("%m/%d/%Y")
    posted_to = datetime.now(UTC).strftime("%m/%d/%Y")

    params = {
        "api_key": settings.sam_gov_api_key,
        "postedFrom": posted_from,
        "postedTo": posted_to,
        "limit": PAGE_SIZE,
        "offset": 0,
    }

    ingested = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.get(SAM_BASE, params=params)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 60))
                    logger.warning(f"SAM.gov rate limited, waiting {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"SAM.gov API error: {e}")
                break

            items = data.get("opportunitiesData") or []
            if not items:
                break

            parsed = [_parse_opportunity(item) for item in items]

            # Generate embeddings for items without one
            texts = [f"{p['title']} {p['description'] or ''}" for p in parsed]
            try:
                embeddings = await embed_batch(texts)
            except Exception as e:
                logger.warning(f"Embedding failed: {e}")
                embeddings = [None] * len(parsed)

            for p, emb in zip(parsed, embeddings):
                p["embedding"] = emb

            async with AsyncSessionLocal() as session:
                for p in parsed:
                    if not p["source_id"]:
                        continue
                    stmt = (
                        insert(Opportunity)
                        .values(**p)
                        .on_conflict_do_update(
                            index_elements=["source_id"],
                            set_={k: v for k, v in p.items() if k != "source_id"},
                        )
                    )
                    await session.execute(stmt)
                await session.commit()

            ingested += len(parsed)
            total = data.get("totalRecords") or 0
            params["offset"] += PAGE_SIZE

            if params["offset"] >= total or params["offset"] >= 10000:
                break

    logger.info(f"SAM.gov sync complete: {ingested} opportunities ingested")
    return ingested
