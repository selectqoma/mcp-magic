"""SBIR/STTR opportunity sync.

Uses the SBIR.gov API:
  GET https://api.sbir.gov/solicitations
"""

import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy.dialects.postgresql import insert

from src.config import settings
from src.db import AsyncSessionLocal
from src.embeddings import embed_batch
from src.models.opportunity import Opportunity

logger = logging.getLogger(__name__)

SBIR_BASE = "https://api.sbir.gov/solicitations"
PAGE_SIZE = 50


def _parse_solicitation(item: dict) -> dict:
    return {
        "source": "sbir",
        "source_id": f"SBIR-{item.get('solicitation_id') or item.get('solicitation_number') or item.get('topic_number', '')}",
        "title": (item.get("program") or "") + " - " + (item.get("agency") or ""),
        "description": item.get("solicitation_topics", ""),
        "agency": item.get("agency", ""),
        "naics_codes": [],
        "set_aside_type": "SB",  # SBIR is always small business
        "posted_date": _parse_date(item.get("open_date")),
        "response_deadline": _parse_date(item.get("close_date")),
        "estimated_value_low": None,
        "estimated_value_high": None,
        "place_of_performance": None,
        "contact_info": {"agency": item.get("agency")},
        "attachments": _parse_attachments(item),
        "raw_data": item,
    }


def _parse_date(val: str | None) -> datetime | None:
    if not val:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(val[:10], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_attachments(item: dict) -> list:
    attachments = []
    if item.get("solicitation_url"):
        attachments.append({
            "filename": "solicitation.pdf",
            "url": item["solicitation_url"],
            "parsed": False,
        })
    return attachments


async def sync_sbir_opportunities() -> int:
    """Fetch SBIR/STTR solicitations and upsert. Returns count ingested."""
    params = {"rows": PAGE_SIZE, "start": 0, "status": "open"}
    ingested = 0

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                resp = await client.get(SBIR_BASE, params=params)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"SBIR.gov API error: {e}")
                break

            items = data if isinstance(data, list) else (data.get("solicitations") or [])
            if not items:
                break

            parsed = [_parse_solicitation(item) for item in items]

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
                    if p["source_id"] != "SBIR-":
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
            if len(items) < PAGE_SIZE:
                break
            params["start"] += PAGE_SIZE

    logger.info(f"SBIR sync complete: {ingested} solicitations ingested")
    return ingested
