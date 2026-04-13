"""SBIR/STTR opportunity sync.

SBIR.gov's public API is no longer accessible (returns 403 for all endpoints).
We source SBIR solicitations from SAM.gov instead, filtering for opportunities
that mention 'SBIR' or 'STTR' — this covers the majority of SBIR/STTR
solicitations which are published on SAM.gov alongside other solicitation types.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.dialects.postgresql import insert

from src.config import settings
from src.db import AsyncSessionLocal
from src.embeddings import embed_batch
from src.models.opportunity import Opportunity
from src.sync.sam import _fetch_description, _noop, _parse_opportunity as _parse_sam_opportunity

logger = logging.getLogger(__name__)

SAM_BASE = "https://api.sam.gov/opportunities/v2/search"
PAGE_SIZE = 100


async def sync_sbir_opportunities() -> int:
    """Fetch SBIR/STTR solicitations from SAM.gov and upsert. Returns count ingested."""
    if not settings.sam_gov_api_key:
        logger.warning("SAM_GOV_API_KEY not set, skipping SBIR sync")
        return 0

    posted_from = (datetime.now(UTC) - timedelta(days=30)).strftime("%m/%d/%Y")
    posted_to = datetime.now(UTC).strftime("%m/%d/%Y")

    params = {
        "api_key": settings.sam_gov_api_key,
        "q": "SBIR STTR",
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
                logger.error(f"SAM.gov SBIR query error: {e}")
                break

            items = data.get("opportunitiesData") or []
            if not items:
                break

            parsed = [_parse_sam_opportunity(item) for item in items]

            # Index as source=sbir with SBIR- prefix so users can filter by source=sbir.
            # These same records may exist as source=sam — that's acceptable; the SBIR
            # source lets users find them specifically as SBIR/STTR solicitations.
            for p in parsed:
                p["source"] = "sbir"
                p["source_id"] = "SBIR-" + p["source_id"]
                if not p.get("set_aside_type"):
                    p["set_aside_type"] = "SB"

            # Fetch real description text where SAM returned a URL
            desc_tasks = [
                _fetch_description(client, p["description"])
                if (p.get("description") or "").startswith("http")
                else _noop()
                for p in parsed
            ]
            fetched_descs = await asyncio.gather(*desc_tasks, return_exceptions=True)
            for p, fetched in zip(parsed, fetched_descs):
                if isinstance(fetched, str) and fetched:
                    p["description"] = fetched

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
                    if p["source_id"] == "SBIR-":
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

    logger.info(f"SBIR sync (via SAM.gov) complete: {ingested} opportunities ingested")
    return ingested
