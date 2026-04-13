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

    trial_str = user.trial_ends_at.strftime("%B %d, %Y") if user.trial_ends_at else None
    # Fire-and-forget: email delivery failure should not fail the registration
    background_tasks.add_task(send_welcome, user.email, user.api_key, trial_str)

    return {
        "api_key": user.api_key,
        "tier": user.tier,
        "trial_ends_at": user.trial_ends_at.isoformat() if user.trial_ends_at else None,
        "message": (
            "Welcome! Your free account includes 5 searches/day and a 14-day Pro trial. "
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


@app.get("/admin/sam-debug")
async def admin_sam_debug(noticeid: str, request: Request):
    """Test SAM API description fetch for a given noticeid."""
    import httpx
    from src.config import settings
    key = settings.sam_gov_api_key
    url = f"https://api.sam.gov/prod/opportunities/v1/noticedesc?noticeid={noticeid}"
    results = {}
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        # Test v1 URL with auth
        try:
            r = await client.get(url, params={"api_key": key})
            results["v1_with_auth"] = {"status": r.status_code, "len": len(r.text), "preview": r.text[:200]}
        except Exception as e:
            results["v1_with_auth"] = {"error": str(e)}
        # Test v2 search by noticeid
        try:
            r = await client.get(
                "https://api.sam.gov/opportunities/v2/search",
                params={"api_key": key, "noticeid": noticeid, "limit": 1},
            )
            data = r.json()
            items = data.get("opportunitiesData") or []
            desc = ""
            if items:
                n = items[0].get("notice") or items[0]
                desc = n.get("description", "")
            results["v2_search"] = {"status": r.status_code, "items": len(items), "desc_preview": str(desc)[:200]}
        except Exception as e:
            results["v2_search"] = {"error": str(e)}
    return results


# ── Static landing page ────────────────────────────────────────────────────────

_LANDING_PATH = Path(__file__).parent.parent / "landing" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def landing():
    try:
        return HTMLResponse(_LANDING_PATH.read_text())
    except FileNotFoundError:
        return HTMLResponse("<h1>GovCon MCP</h1><p>Coming soon.</p>")
