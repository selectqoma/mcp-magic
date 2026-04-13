"""Opportunity search service — combines semantic (pgvector) + SQL filters."""

from datetime import UTC, datetime

from sqlalchemy import and_, cast, func, or_, select, text
from sqlalchemy.dialects.postgresql import ARRAY, VARCHAR
from sqlalchemy.ext.asyncio import AsyncSession

from src.embeddings import embed_text
from src.models.opportunity import Opportunity

# Cosine distance threshold for semantic search (lower = more similar).
# 0.45 ≈ similarity score of 0.55 — keeps clearly relevant results, cuts noise.
SIMILARITY_THRESHOLD = 0.65


async def search_opportunities(
    session: AsyncSession,
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
    filters = []

    # Only active opportunities (deadline in the future or no deadline)
    filters.append(
        or_(
            Opportunity.response_deadline >= datetime.now(UTC),
            Opportunity.response_deadline.is_(None),
        )
    )

    if source:
        filters.append(Opportunity.source == source.lower())
    if agency:
        filters.append(Opportunity.agency.ilike(f"%{agency}%"))
    if set_aside_type:
        filters.append(Opportunity.set_aside_type.ilike(f"%{set_aside_type}%"))
    if naics_codes:
        filters.append(
            Opportunity.naics_codes.overlap(cast(naics_codes, ARRAY(VARCHAR)))
        )
    if value_min is not None:
        filters.append(
            or_(
                Opportunity.estimated_value_high >= value_min,
                Opportunity.estimated_value_low >= value_min,
            )
        )
    if value_max is not None:
        filters.append(
            or_(
                Opportunity.estimated_value_low <= value_max,
                Opportunity.estimated_value_low.is_(None),
            )
        )
    if deadline_after:
        try:
            dt = datetime.fromisoformat(deadline_after).replace(tzinfo=UTC)
            filters.append(Opportunity.response_deadline >= dt)
        except ValueError:
            pass
    if deadline_before:
        try:
            dt = datetime.fromisoformat(deadline_before).replace(tzinfo=UTC)
            filters.append(Opportunity.response_deadline <= dt)
        except ValueError:
            pass

    where_clause = and_(*filters) if filters else text("true")

    if query:
        # IVFFlat index with 100 lists needs higher probes for small datasets.
        # probes=50 means we search 50% of clusters — much better recall at cost of speed.
        await session.execute(text("SET LOCAL ivfflat.probes = 50"))

        # Semantic search: filter + order by cosine distance, threshold cuts irrelevant results
        embedding = await embed_text(query)
        emb_str = f"[{','.join(str(x) for x in embedding)}]"
        distance_expr = text("embedding <=> CAST(:q_vec AS vector)").bindparams(q_vec=emb_str)
        semantic_filter = and_(
            where_clause,
            Opportunity.embedding.is_not(None),
            text("embedding <=> CAST(:q_vec2 AS vector) < :threshold").bindparams(
                q_vec2=emb_str, threshold=SIMILARITY_THRESHOLD
            ),
        )
        stmt = (
            select(Opportunity)
            .where(semantic_filter)
            .order_by(distance_expr)
            .limit(limit)
            .offset(offset)
        )
        count_stmt = select(func.count()).select_from(Opportunity).where(semantic_filter)
    else:
        stmt = (
            select(Opportunity)
            .where(where_clause)
            .order_by(Opportunity.response_deadline.asc().nulls_last())
            .limit(limit)
            .offset(offset)
        )
        count_stmt = select(func.count()).select_from(Opportunity).where(where_clause)

    result = await session.execute(stmt)
    opps = result.scalars().all()

    total = (await session.execute(count_stmt)).scalar() or 0

    return {
        "results": [_serialize(o) for o in opps],
        "total_count": total,
        "has_more": offset + limit < total,
    }


def _extract_date_from_raw(raw: dict | None, *field_names: str):
    """Pull a datetime from raw_data fields when the DB column is null."""
    if not raw:
        return None
    notice = raw.get("notice") or raw
    for field in field_names:
        val = notice.get(field)
        if val:
            from src.sync.sam import _parse_date as _sam_parse_date
            try:
                return _sam_parse_date(str(val))
            except Exception:
                pass
    return None


def _serialize(o: Opportunity) -> dict:
    posted_date = o.posted_date
    response_deadline = o.response_deadline

    # Fall back to raw_data for null dates (backfill SAM records often have dates only there)
    if o.source == "sam" and o.raw_data:
        if not posted_date:
            posted_date = _extract_date_from_raw(o.raw_data, "postedDate", "publishedDate")
        if not response_deadline:
            response_deadline = _extract_date_from_raw(o.raw_data, "responseDeadLine", "archiveDate")

    # has_parsed_requirements is true only when requirements parsed successfully (not error)
    has_parsed = (
        o.requirements is not None
        and not (isinstance(o.requirements, dict) and "error" in o.requirements)
    )

    return {
        "id": str(o.id),
        "source_id": o.source_id,
        "source": o.source,
        "title": o.title,
        "agency": o.agency,
        "naics_codes": o.naics_codes,
        "set_aside_type": o.set_aside_type,
        "posted_date": posted_date.isoformat() if posted_date else None,
        "deadline": response_deadline.isoformat() if response_deadline else None,
        "estimated_value_low": float(o.estimated_value_low) if o.estimated_value_low else None,
        "estimated_value_high": float(o.estimated_value_high) if o.estimated_value_high else None,
        "place_of_performance": o.place_of_performance,
        "contact_info": o.contact_info,
        # Strip numeric placeholder filenames ("0", "1") — real filenames are resolved lazily
        # in get_opportunity_details via the SAM v3 resources API. Show attachment_count instead.
        "attachments": [
            {k: v for k, v in a.items() if k != "filename" or not str(v).isdigit()}
            for a in (o.attachments or [])
        ],
        "attachment_count": len(o.attachments or []),
        "has_parsed_requirements": has_parsed,
    }
