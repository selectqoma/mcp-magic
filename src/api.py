"""FastAPI application — handles registration, billing webhooks, and health."""

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr

from src.auth import create_user
from src.billing import handle_paddle_webhook, verify_paddle_webhook
from src.db import AsyncSessionLocal
from src.email import send_resend_api_key, send_welcome
from src.models.user import User

logger = logging.getLogger(__name__)

app = FastAPI(title="GovCon MCP API", docs_url=None, redoc_url=None)

# In-process rate limiters (IP-keyed, not Redis — single-worker only; see SETUP.md)
_register_attempts: dict[str, list[datetime]] = defaultdict(list)
_resend_attempts: dict[str, list[datetime]] = defaultdict(list)

REGISTER_LIMIT = 5
REGISTER_WINDOW = timedelta(hours=1)

RESEND_KEY_LIMIT = 3
RESEND_KEY_WINDOW = timedelta(hours=1)


def _check_rate_limit(
    store: dict[str, list[datetime]],
    key: str,
    limit: int,
    window: timedelta,
) -> bool:
    now = datetime.now(UTC)
    store[key] = [t for t in store[key] if now - t < window]
    if len(store[key]) >= limit:
        return False
    store[key].append(now)
    return True


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── User registration ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    company_name: str | None = None


@app.post("/register")
async def register(req: RegisterRequest, request: Request, background_tasks: BackgroundTasks):
    """Create a free account and return an API key."""
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(_register_attempts, ip, REGISTER_LIMIT, REGISTER_WINDOW):
        raise HTTPException(status_code=429, detail="Too many registration attempts. Try again later.")

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        existing = await session.execute(select(User).where(User.email == req.email))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="An account with this email already exists. Use /resend-key to retrieve your API key.")

        user = await create_user(req.email, session)

    # Fire-and-forget: email delivery failure should not fail the registration
    background_tasks.add_task(send_welcome, user.email, user.api_key, None)

    return {
        "api_key": user.api_key,
        "tier": user.tier,
        "message": (
            "Welcome! Your free account includes 5 searches/day. "
            "Add this key to your MCP config to get started."
        ),
        "mcp_config": {
            "mcpServers": {
                "govcon-mcp": {
                    "command": "npx",
                    "args": ["-y", "mcp-remote", "https://api.govconmcp.com/mcp/"],
                    "env": {"MCP_API_KEY": user.api_key},
                }
            }
        },
    }


# ── Resend API key ─────────────────────────────────────────────────────────────

class ResendKeyRequest(BaseModel):
    email: EmailStr


@app.post("/resend-key")
async def resend_key(req: ResendKeyRequest, request: Request, background_tasks: BackgroundTasks):
    """Re-deliver the API key for an existing account to the registered email.

    Rate-limited separately from /register to prevent shared-IP lockout.
    Always returns 200 regardless of whether the account exists (anti-enumeration).
    """
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(_resend_attempts, ip, RESEND_KEY_LIMIT, RESEND_KEY_WINDOW):
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.email == req.email))
        user = result.scalar_one_or_none()

    # Fire-and-forget in background — constant response time regardless of email existence
    if user:
        background_tasks.add_task(send_resend_api_key, user.email, user.api_key)

    return {"message": "If an account exists for that email, your API key has been sent."}


# ── Paddle webhook ─────────────────────────────────────────────────────────────

@app.post("/webhooks/paddle")
async def paddle_webhook(request: Request):
    body = await request.body()
    if len(body) > 1_000_000:  # 1MB max
        raise HTTPException(status_code=413, detail="Payload too large")

    signature = request.headers.get("Paddle-Signature", "")
    if not verify_paddle_webhook(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = event.get("event_type", "")
    data = event.get("data", {})

    async with AsyncSessionLocal() as session:
        await handle_paddle_webhook(event_type, data, session)

    return Response(status_code=200)


# ── Admin: trigger backfill ────────────────────────────────────────────────────

@app.post("/admin/backfill")
async def admin_backfill(request: Request, background_tasks: BackgroundTasks):
    """Trigger SAM.gov backfill (last 90 days). Protected by ADMIN_SECRET env var."""
    from src.config import settings
    secret = request.headers.get("X-Admin-Secret", "")
    if not secret or secret != getattr(settings, "admin_secret", None):
        raise HTTPException(status_code=403, detail="Forbidden")
    from src.sync.sam import backfill_sam_opportunities
    background_tasks.add_task(backfill_sam_opportunities, days_back=90, chunk_days=7)
    return {"message": "Backfill started in background (90 days, 7-day chunks)"}


@app.post("/admin/backfill-awards")
async def admin_backfill_awards(request: Request, background_tasks: BackgroundTasks):
    """Trigger USASpending 90-day backfill. Protected by ADMIN_SECRET."""
    from src.config import settings
    secret = request.headers.get("X-Admin-Secret", "")
    if not secret or secret != getattr(settings, "admin_secret", None):
        raise HTTPException(status_code=403, detail="Forbidden")
    from src.sync.usaspending import backfill_usaspending_awards
    background_tasks.add_task(backfill_usaspending_awards, days_back=90)
    return {"message": "USASpending 90-day backfill started in background"}


@app.get("/admin/sam-debug")
async def admin_sam_debug(noticeid: str, request: Request):
    """Single v2 search call — shows all text fields to diagnose description availability."""
    import httpx
    from src.config import settings
    secret = request.headers.get("X-Admin-Secret", "")
    if not secret or secret != getattr(settings, "admin_secret", None):
        raise HTTPException(status_code=403, detail="Forbidden")
    key = settings.sam_gov_api_key
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            r = await client.get(
                "https://api.sam.gov/opportunities/v2/search",
                params={"api_key": key, "noticeid": noticeid, "limit": 1},
            )
            if r.status_code == 429:
                return {"error": "quota_exhausted", "retry_after": r.headers.get("Retry-After")}
            data = r.json()
            items = data.get("opportunitiesData") or []
            if not items:
                return {"status": r.status_code, "items": 0}
            n = items[0].get("notice") or items[0]
            # Return all text-like fields so we can see what has actual content
            text_fields = {
                k: str(v)[:300]
                for k, v in n.items()
                if isinstance(v, str) and v and k in (
                    "description", "synopsis", "additionalInfoLink",
                    "uiLink", "title", "solicitationNumber", "typeOfNotice",
                )
            }
            return {"status": r.status_code, "items": len(items), "text_fields": text_fields}
        except Exception as e:
            return {"error": str(e)}


@app.get("/admin/sample-descriptions")
async def admin_sample_descriptions(request: Request, limit: int = 5):
    """Return sample raw description values from SAM opps (to debug enrichment)."""
    from src.config import settings
    secret = request.headers.get("X-Admin-Secret", "")
    if not secret or secret != getattr(settings, "admin_secret", None):
        raise HTTPException(status_code=403, detail="Forbidden")
    from sqlalchemy import text as _text
    from src.db import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        result = await session.execute(_text(
            "SELECT source_id, description FROM opportunities "
            "WHERE source='sam' AND description IS NOT NULL LIMIT :n"
        ), {"n": limit})
        rows = result.fetchall()
    return [{"source_id": r[0], "description": r[1][:200] if r[1] else None} for r in rows]


@app.post("/admin/enrich-descriptions")
async def admin_enrich_descriptions(request: Request, background_tasks: BackgroundTasks, batch_size: int = 50):
    """Trigger SAM description enrichment. Protected by ADMIN_SECRET."""
    from src.config import settings
    secret = request.headers.get("X-Admin-Secret", "")
    if not secret or secret != getattr(settings, "admin_secret", None):
        raise HTTPException(status_code=403, detail="Forbidden")
    from src.sync.sam import enrich_sam_descriptions
    background_tasks.add_task(enrich_sam_descriptions, batch_size=batch_size)
    return {"message": f"Description enrichment started (batch_size={batch_size})"}


@app.get("/admin/description-stats")
async def admin_description_stats(request: Request):
    """Show how many SAM opps have null/URL/real descriptions. Protected by ADMIN_SECRET."""
    from src.config import settings
    secret = request.headers.get("X-Admin-Secret", "")
    if not secret or secret != getattr(settings, "admin_secret", None):
        raise HTTPException(status_code=403, detail="Forbidden")
    from sqlalchemy import func, case, text as _text
    from src.db import AsyncSessionLocal
    from src.models.opportunity import Opportunity
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            _text("""
                SELECT
                    COUNT(*) FILTER (WHERE source = 'sam' AND description IS NULL) AS sam_null,
                    COUNT(*) FILTER (WHERE source = 'sam' AND description = '') AS sam_empty,
                    COUNT(*) FILTER (WHERE source = 'sam' AND description LIKE 'http%') AS sam_url,
                    COUNT(*) FILTER (WHERE source = 'sam' AND description NOT LIKE 'http%' AND description IS NOT NULL AND description != '') AS sam_real,
                    COUNT(*) FILTER (WHERE source = 'sam') AS sam_total
                FROM opportunities
            """)
        )
        row = result.fetchone()
        return {
            "sam_null": row[0],
            "sam_empty": row[1],
            "sam_url": row[2],
            "sam_real": row[3],
            "sam_total": row[4],
        }


# ── Smithery / MCP discovery ───────────────────────────────────────────────────

@app.get("/.well-known/mcp/server-card.json")
async def mcp_server_card():
    """Static metadata for Smithery and other MCP discovery tools."""
    return {
        "name": "GovCon MCP",
        "displayName": "GovCon MCP — Federal Contract Intelligence",
        "description": (
            "Search US federal contract and grant opportunities from SAM.gov, Grants.gov, "
            "and SBIR.gov. Parse solicitation PDFs, score opportunities against your company "
            "profile, and track competitor award history via USASpending.gov."
        ),
        "homepage": "https://govconmcp.com",
        "repository": "https://github.com/selectqoma/mcp-magic",
        "transport": "http",
        "endpoint": "https://api.govconmcp.com/mcp/",
        "auth": {
            "type": "apiKey",
            "description": "Pass your API key as api_key parameter in each tool call. Get one free at govconmcp.com.",
        },
        "categories": ["business", "government", "search", "data"],
        "pricing": {
            "model": "freemium",
            "free": "5 searches/day, no credit card",
            "pro": "$49/mo — unlimited searches, PDF parsing, profile matching, competitor intel",
            "agency": "$199/mo — everything in Pro plus 20 profiles and webhook alerts",
        },
    }


# ── Static landing page ────────────────────────────────────────────────────────

_LANDING_PATH = Path(__file__).parent.parent / "landing" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def landing():
    try:
        return HTMLResponse(_LANDING_PATH.read_text())
    except FileNotFoundError:
        return HTMLResponse("<h1>GovCon MCP</h1><p>Coming soon.</p>")
