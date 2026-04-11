"""USASpending.gov award sync.

Uses the USASpending API v2:
  POST https://api.usaspending.gov/api/v2/search/spending_by_award/
  Docs: https://api.usaspending.gov/
"""

import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.dialects.postgresql import insert

from src.db import AsyncSessionLocal
from src.models.award import Award

logger = logging.getLogger(__name__)

USA_BASE = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
PAGE_SIZE = 100


def _parse_award(item: dict) -> dict:
    return {
        "award_id": item.get("Award ID") or item.get("award_id", ""),
        "recipient_name": item.get("Recipient Name") or item.get("recipient_name"),
        "recipient_uei": item.get("recipient_uei"),
        "recipient_cage": item.get("recipient_cage_code"),
        "agency": item.get("Awarding Agency") or item.get("awarding_agency_name"),
        "sub_agency": item.get("Awarding Sub Agency") or item.get("awarding_sub_agency_name"),
        "title": item.get("Award Description") or item.get("description"),
        "naics_code": item.get("NAICS Code") or item.get("naics_code"),
        "award_amount": _parse_float(item.get("Award Amount") or item.get("total_obligated_amount")),
        "award_date": _parse_date(item.get("Start Date") or item.get("action_date")),
        "period_of_performance_end": _parse_date(item.get("End Date")),
        "place_of_performance": item.get("Place of Performance State Code"),
        "raw_data": item,
    }


def _parse_date(val: str | None) -> datetime | None:
    if not val:
        return None
    try:
        return datetime.strptime(val[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_float(val) -> float | None:
    try:
        return float(str(val).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


async def sync_usaspending_awards() -> int:
    """Fetch recent federal contract awards. Returns count ingested."""
    date_range_start = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
    date_range_end = datetime.now(UTC).strftime("%Y-%m-%d")

    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],  # contracts only
            "time_period": [{"start_date": date_range_start, "end_date": date_range_end}],
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount", "Awarding Agency",
            "Awarding Sub Agency", "Award Description", "NAICS Code",
            "Start Date", "End Date", "Place of Performance State Code",
        ],
        "page": 1,
        "limit": PAGE_SIZE,
        "sort": "Award Amount",
        "order": "desc",
    }

    ingested = 0
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            try:
                resp = await client.post(USA_BASE, json=payload)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"USASpending API error: {e}")
                break

            items = data.get("results") or []
            if not items:
                break

            parsed = [_parse_award(item) for item in items]

            async with AsyncSessionLocal() as session:
                for p in parsed:
                    if not p["award_id"]:
                        continue
                    stmt = (
                        insert(Award)
                        .values(**p)
                        .on_conflict_do_update(
                            index_elements=["award_id"],
                            set_={k: v for k, v in p.items() if k != "award_id"},
                        )
                    )
                    await session.execute(stmt)
                await session.commit()

            ingested += len(parsed)
            page_meta = data.get("page_metadata") or {}
            if not page_meta.get("hasNext") and not page_meta.get("has_next_page"):
                break
            payload["page"] += 1

    logger.info(f"USASpending sync complete: {ingested} awards ingested")
    return ingested
