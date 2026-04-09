"""FastAPI application — handles registration, billing webhooks, and health.

The MCP server (FastMCP/SSE) is mounted at /sse.
The REST API handles user-facing flows that don't happen inside MCP tool calls.
"""

import json
import logging
import secrets

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr

from src.auth import create_user
from src.billing import handle_paddle_webhook, verify_paddle_webhook
from src.db import AsyncSessionLocal
from src.models.user import User

logger = logging.getLogger(__name__)

app = FastAPI(title="MCP Magic API", docs_url=None, redoc_url=None)


# ── Health check (Railway uses this) ──────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── User registration ──────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    company_name: str | None = None


@app.post("/register")
async def register(req: RegisterRequest):
    """Create a free account and return an API key."""
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        existing = await session.execute(
            select(User).where(User.email == req.email)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already registered.")

        user = await create_user(req.email, session)

    return {
        "api_key": user.api_key,
        "tier": user.tier,
        "trial_ends_at": user.trial_ends_at.isoformat() if user.trial_ends_at else None,
        "message": (
            "Welcome! Your free account includes 20 searches/day and a 14-day Pro trial. "
            "Add this key to your MCP config to get started."
        ),
        "mcp_config": {
            "mcpServers": {
                "mcp-magic": {
                    "command": "npx",
                    "args": ["-y", "mcp-remote", "https://api.mcpmagic.io/sse"],
                    "env": {"MCP_API_KEY": user.api_key},
                }
            }
        },
    }


# ── Paddle webhook ─────────────────────────────────────────────────────────────

@app.post("/webhooks/paddle")
async def paddle_webhook(request: Request):
    body = await request.body()
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


# ── Static landing page ────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing():
    try:
        with open("landing/index.html") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>MCP Magic</h1><p>Coming soon.</p>")
