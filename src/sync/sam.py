"""SAM.gov opportunity sync.

Uses the beta.SAM.gov API v2:
  GET https://api.sam.gov/opportunities/v2/search
  Docs: https://open.gsa.gov/api/get-opportunities-public-api/

Register for an API key at: https://sam.gov/profile/details
"""

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser

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
_DESC_SEMAPHORE = asyncio.Semaphore(5)


class _HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts).strip()


def _strip_html(html: str) -> str:
    p = _HTMLStripper()
    p.feed(html)
    text = p.get_text()
    # Collapse whitespace
    return re.sub(r"\s+", " ", text).strip()


async def _fetch_description(client: httpx.AsyncClient, url: str) -> str:
    """Fetch SAM.gov notice description HTML and return as plain text."""
    async with _DESC_SEMAPHORE:
        try:
            resp = await client.get(
                url,
                params={"api_key": settings.sam_gov_api_key},
                timeout=15,
            )
            if resp.status_code == 200:
                return _strip_html(resp.text)[:3000]
        except Exception as e:
            logger.debug(f"Failed to fetch SAM description from {url}: {e}")
    return ""


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
    # Map format → expected output length (format string length ≠ output length)
    _FMT_LENS = {
        "%Y-%m-%dT%H:%M:%S%z": 25,
        "%Y-%m-%dT%H:%M:%S": 19,
        "%Y-%m-%d %H:%M:%S": 19,
        "%Y-%m-%d": 10,
        "%m/%d/%Y": 10,
    }
    for fmt, flen in _FMT_LENS.items():
        try:
            parsed = datetime.strptime(val[:flen], fmt)
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


async def _noop() -> str:
    return ""


async def _sync_date_range(client: httpx.AsyncClient, posted_from: str, posted_to: str) -> int:
    """Fetch and upsert all SAM.gov opportunities in a date range. Returns count ingested."""
    params = {
        "api_key": settings.sam_gov_api_key,
        "postedFrom": posted_from,
        "postedTo": posted_to,
        "limit": PAGE_SIZE,
        "offset": 0,
    }
    ingested = 0

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

        # Fetch real description text for items where SAM returned a URL
        desc_tasks = [
            _fetch_description(client, p["description"])
            if (p.get("description") or "").startswith("http")
            else _noop()  # description already plain text or empty
            for p in parsed
        ]
        fetched_descs = await asyncio.gather(*desc_tasks, return_exceptions=True)
        for p, fetched in zip(parsed, fetched_descs):
            if isinstance(fetched, str) and fetched:
                p["description"] = fetched

        # Generate embeddings — title + description + agency + set_aside for richer signal
        texts = [
            " ".join(filter(None, [
                p["title"],
                p.get("description") or "",
                p.get("agency") or "",
                p.get("set_aside_type") or "",
            ]))
            for p in parsed
        ]
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

    return ingested


async def sync_sam_opportunities() -> int:
    """Fetch recent SAM.gov opportunities (last 14 days) and upsert. Returns count ingested."""
    if not settings.sam_gov_api_key:
        logger.warning("SAM_GOV_API_KEY not set, skipping sync")
        return 0

    posted_from = (datetime.now(UTC) - timedelta(days=14)).strftime("%m/%d/%Y")
    posted_to = datetime.now(UTC).strftime("%m/%d/%Y")

    async with httpx.AsyncClient(timeout=30) as client:
        ingested = await _sync_date_range(client, posted_from, posted_to)

    logger.info(f"SAM.gov sync complete: {ingested} opportunities ingested")
    return ingested


async def backfill_sam_opportunities(days_back: int = 365, chunk_days: int = 7) -> int:
    """Backfill SAM.gov opportunities going back `days_back` days in `chunk_days` chunks.

    SAM.gov caps results at 10,000 per query, so we use narrow date windows.
    Call once manually to populate the database with historical data.
    """
    if not settings.sam_gov_api_key:
        logger.warning("SAM_GOV_API_KEY not set, skipping backfill")
        return 0

    total_ingested = 0
    now = datetime.now(UTC)
    # Start from oldest and work forward, skipping the last 2 days (covered by regular sync)
    start = now - timedelta(days=days_back)
    end = now - timedelta(days=2)

    async with httpx.AsyncClient(timeout=60) as client:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=chunk_days), end)
            pf = cursor.strftime("%m/%d/%Y")
            pt = chunk_end.strftime("%m/%d/%Y")
            logger.info(f"Backfill: {pf} → {pt}")
            count = await _sync_date_range(client, pf, pt)
            total_ingested += count
            logger.info(f"  ingested {count} (total so far: {total_ingested})")
            cursor = chunk_end + timedelta(days=1)

    logger.info(f"SAM.gov backfill complete: {total_ingested} total opportunities ingested")
    return total_ingested
