"""Profile-to-opportunity matching service.

Scoring breakdown:
  - Keyword overlap (45%): do the company's capabilities appear in the opportunity?
  - NAICS match (25%): do NAICS codes overlap?
  - Semantic similarity (20%): cosine similarity between profile embedding and opportunity
  - Set-aside eligibility (10%): does the company hold the required certification?

Set-aside weight is intentionally low (10%) — it's a pass/fail gate, not a relevance signal.
Keeping it low prevents certified companies from seeing irrelevant opps just because they're
eligible. Keyword + NAICS (70% combined) must drive the match.
"""

import logging

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.embeddings import embed_text
from src.models.opportunity import Opportunity
from src.models.profile import CompanyProfile

logger = logging.getLogger(__name__)

# Set-aside type → required certification mapping
SET_ASIDE_CERT_MAP = {
    "8a": "8a",
    "8(a)": "8a",
    "hubzone": "hubzone",
    "sdvosb": "sdvosb",
    "vosb": "vosb",
    "wosb": "wosb",
    "edwosb": "wosb",
    "sb": "sb",  # small business — anyone qualifies
    "sba": "sb",
    "total small business": "sb",
}


async def match_opportunities_to_profile(
    session: AsyncSession,
    profile: CompanyProfile | None = None,
    capabilities: list[str] | None = None,
    naics_codes: list[str] | None = None,
    certifications: list[str] | None = None,
    past_agencies: list[str] | None = None,
    max_results: int = 10,
    min_score: float = 0.25,
) -> dict:
    # Merge inline params with saved profile
    caps = list(capabilities or []) + list(getattr(profile, "capabilities", None) or [])
    naics = list(naics_codes or []) + list(getattr(profile, "naics_codes", None) or [])
    certs = list(certifications or []) + list(getattr(profile, "certifications", None) or [])
    agencies = list(past_agencies or []) + list(getattr(profile, "past_agencies", None) or [])

    if not any([caps, naics, certs]):
        return {"error": "Provide at least capabilities, naics_codes, or certifications."}

    # Build profile text for semantic embedding
    profile_text = " ".join(caps + naics + certs + agencies)
    try:
        profile_embedding = await embed_text(profile_text)
    except Exception as e:
        logger.warning(f"Could not embed profile: {e}")
        profile_embedding = None

    # Fetch candidate opportunities (active, non-expired)
    from datetime import UTC, datetime
    stmt = (
        select(Opportunity)
        .where(
            or_(
                Opportunity.response_deadline >= datetime.now(UTC),
                Opportunity.response_deadline.is_(None),
            )
        )
        .limit(500)  # score top 500 candidates
    )

    result = await session.execute(stmt)
    opps = result.scalars().all()

    if not opps:
        return {"matches": [], "message": "No active opportunities found in database yet."}

    scored = []
    for opp in opps:
        score, breakdown = _score_opportunity(opp, caps, naics, certs, profile_embedding)
        if score >= min_score:
            scored.append((score, breakdown, opp))

    scored.sort(key=lambda x: x[0], reverse=True)
    scored = scored[:max_results]

    matches = []
    for score, breakdown, opp in scored:
        matches.append({
            "opportunity_id": str(opp.id),
            "title": opp.title,
            "agency": opp.agency,
            "source": opp.source,
            "deadline": opp.response_deadline.isoformat() if opp.response_deadline else None,
            "set_aside_type": opp.set_aside_type,
            "estimated_value_low": float(opp.estimated_value_low) if opp.estimated_value_low else None,
            "score": round(score, 3),
            "score_breakdown": breakdown,
            "reasoning": _generate_reasoning(breakdown, opp, caps, naics, certs),
        })

    return {"matches": matches}


def _score_opportunity(
    opp: Opportunity,
    caps: list[str],
    naics: list[str],
    certs: list[str],
    profile_embedding: list[float] | None,
) -> tuple[float, dict]:
    keyword_score = _keyword_score(opp, caps)
    naics_score = _naics_score(opp, naics)
    setaside_score = _setaside_score(opp, certs)
    semantic_score = _semantic_score(opp, profile_embedding)

    # Hard relevance gate: set-aside eligibility must not substitute for actual relevance.
    # If keyword, naics, and semantic all signal low/no match, exclude regardless of set-aside.
    # This prevents "NUT, SELF-LOCKING" from appearing in cyber company results just because
    # the company is SBA-certified.
    relevance_score = keyword_score * 0.45 + naics_score * 0.25 + semantic_score * 0.20
    if relevance_score < 0.12:
        return 0.0, {
            "keyword": round(keyword_score, 3),
            "naics": round(naics_score, 3),
            "set_aside_eligibility": round(setaside_score, 3),
            "semantic": round(semantic_score, 3),
        }

    total = (
        keyword_score * 0.45
        + naics_score * 0.25
        + setaside_score * 0.10
        + semantic_score * 0.20
    )

    return total, {
        "keyword": round(keyword_score, 3),
        "naics": round(naics_score, 3),
        "set_aside_eligibility": round(setaside_score, 3),
        "semantic": round(semantic_score, 3),
    }


def _keyword_score(opp: Opportunity, caps: list[str]) -> float:
    if not caps:
        return 0.5  # neutral when no capabilities provided
    text = f"{opp.title or ''} {opp.description or ''}".lower()
    matches = sum(1 for cap in caps if cap.lower() in text)
    return min(matches / len(caps), 1.0)


def _naics_score(opp: Opportunity, naics: list[str]) -> float:
    if not naics:
        return 0.5  # profile has no NAICS preference — neutral
    if not opp.naics_codes:
        return 0.3  # opp has no NAICS data — unknown relevance, slight penalty
    opp_naics = set(opp.naics_codes or [])
    profile_naics = set(naics)
    # Also match on 4-digit prefix (broader category)
    prefix_matches = any(
        any(p.startswith(o[:4]) for p in profile_naics) for o in opp_naics
    )
    if opp_naics & profile_naics:
        return 1.0
    if prefix_matches:
        return 0.6
    return 0.0


def _setaside_score(opp: Opportunity, certs: list[str]) -> float:
    if not opp.set_aside_type:
        return 0.5  # unrestricted — open to all, neutral score
    sa = opp.set_aside_type.lower()
    required_cert = next(
        (v for k, v in SET_ASIDE_CERT_MAP.items() if k in sa), None
    )
    if not required_cert:
        return 0.5  # unknown set-aside type

    certs_lower = [c.lower() for c in certs]
    if required_cert in certs_lower or "sb" in certs_lower:
        return 1.0
    return 0.0  # missing required certification


def _semantic_score(opp: Opportunity, profile_embedding: list[float] | None) -> float:
    if profile_embedding is None or opp.embedding is None:
        return 0.5  # neutral when embeddings unavailable
    import numpy as np
    a = np.array(profile_embedding)
    b = np.array(opp.embedding)
    cosine = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))
    # Text embeddings (text-embedding-3-small) produce non-negative cosine values.
    # Use raw cosine directly — don't inflate with [-1,1]→[0,1] normalization.
    return max(0.0, cosine)


def _generate_reasoning(breakdown: dict, opp: Opportunity, caps: list, naics: list, certs: list) -> str:
    parts = []
    if breakdown["keyword"] >= 0.5:
        matched = [cap for cap in caps if cap.lower() in f"{opp.title or ''} {opp.description or ''}".lower()]
        if matched:
            parts.append(f"keywords matched: {', '.join(matched[:3])}")
        else:
            parts.append("general capability relevance")
    elif breakdown["keyword"] < 0.2 and caps:
        parts.append("low keyword overlap with your capabilities")
    if breakdown["naics"] == 1.0:
        parts.append(f"exact NAICS match ({', '.join(opp.naics_codes or [])})")
    elif breakdown["naics"] == 0.6:
        parts.append("NAICS sector overlap")
    elif breakdown["naics"] == 0.0 and naics:
        parts.append("no NAICS overlap")
    if breakdown["set_aside_eligibility"] == 1.0 and opp.set_aside_type:
        parts.append(f"eligible: {opp.set_aside_type} set-aside")
    elif breakdown["set_aside_eligibility"] == 0.0 and opp.set_aside_type:
        parts.append(f"ineligible: requires {opp.set_aside_type} certification")
    if breakdown["semantic"] >= 0.65:
        parts.append("high semantic similarity to your profile")
    elif breakdown["semantic"] < 0.45:
        parts.append("low semantic similarity")
    return "; ".join(parts) if parts else "weak match across all criteria"
