"""Grants.gov opportunity sync.

Uses the Grants.gov REST API v2:
  POST https://apply07.grants.gov/grantsws/rest/opportunities/search/
  Docs: https://www.grants.gov/api-reference
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.dialects.postgresql import insert

from src.db import AsyncSessionLocal
from src.embeddings import embed_batch
from src.models.opportunity import Opportunity

logger = logging.getLogger(__name__)

GRANTS_BASE = "https://apply07.grants.gov/grantsws/rest/opportunities/search/"
GRANTS_DETAIL_URL = "https://apply07.grants.gov/grantsws/rest/opportunity/details"
PAGE_SIZE = 100
_DETAIL_SEMAPHORE = asyncio.Semaphore(15)


async def _fetch_grant_description(client: httpx.AsyncClient, source_id: str) -> tuple[str, str]:
    """Fetch synopsis from the Grants.gov detail endpoint. Returns (source_id, description).

    The detail endpoint requires POST with application/x-www-form-urlencoded.
    Response: {"synopsis": {"synopsisDesc": "..."}}
    """
    numeric_id = source_id.removeprefix("GRANTS-")
    async with _DETAIL_SEMAPHORE:
        try:
            resp = await client.post(
                GRANTS_DETAIL_URL,
                content=f"oppId={numeric_id}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            synopsis = data.get("synopsis") or {}
            if isinstance(synopsis, dict):
                text = synopsis.get("synopsisDesc", "") or ""
            else:
                text = str(synopsis)
            return source_id, text.strip()
        except Exception as e:
            logger.debug("Failed to fetch detail for %s: %s", source_id, e)
            return source_id, ""


def _parse_grant(item: dict) -> dict:
    return {
        "source": "grants",
        "source_id": f"GRANTS-{item.get('id', '')}",
        "title": item.get("title", ""),
        "description": "",  # fetched separately via detail endpoint
        "agency": item.get("agency") or item.get("agencyCode", ""),
        "naics_codes": [],
        "set_aside_type": None,
        "posted_date": _parse_date(item.get("openDate")),
        "response_deadline": _parse_date(item.get("closeDate")),
        "estimated_value_low": _parse_float(item.get("awardFloor")),
        "estimated_value_high": _parse_float(item.get("awardCeiling")),
        "place_of_performance": None,
        "contact_info": {"agency": item.get("agency") or item.get("agencyCode")},
        "attachments": [],
        "raw_data": item,
    }


def _parse_date(val: str | None) -> datetime | None:
    if not val:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(val[:10], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_float(val) -> float | None:
    try:
        return float(str(val).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


async def sync_grants_opportunities() -> int:
    """Fetch recent Grants.gov opportunities and upsert. Returns count ingested."""
    close_date_start = (datetime.now(UTC) - timedelta(days=7)).strftime("%m/%d/%Y")
    close_date_end = (datetime.now(UTC) + timedelta(days=180)).strftime("%m/%d/%Y")

    payload = {
        "rows": PAGE_SIZE,
        "startRecordNum": 0,
        "oppStatuses": "forecasted|posted",
        "closeDateRange": f"{close_date_start}|{close_date_end}",
    }

    ingested = 0
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.post(GRANTS_BASE, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"Grants.gov API error: {e}")
                break

            items = data.get("oppHits") or []
            if not items:
                break

            parsed = [_parse_grant(item) for item in items]

            # Fetch descriptions for grants that have none (search API doesn't include synopsis)
            needs_desc = [p for p in parsed if not p.get("description")]
            if needs_desc:
                desc_tasks = [_fetch_grant_description(client, p["source_id"]) for p in needs_desc]
                desc_results = await asyncio.gather(*desc_tasks, return_exceptions=True)
                desc_map = {sid: desc for sid, desc in desc_results if isinstance(desc, str) and desc}
                for p in parsed:
                    if not p.get("description") and p["source_id"] in desc_map:
                        p["description"] = desc_map[p["source_id"]]
                logger.info("Grants.gov: fetched descriptions for %d/%d grants", len(desc_map), len(needs_desc))

            texts = [
                " ".join(filter(None, [
                    p["title"],
                    p.get("description") or "",
                    p.get("agency") or "",
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
                    if not p["source_id"].endswith("-"):
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
            total = data.get("hitCount") or 0
            payload["startRecordNum"] += PAGE_SIZE

            if payload["startRecordNum"] >= total:
                break

    logger.info(f"Grants.gov sync complete: {ingested} opportunities ingested")
    return ingested
