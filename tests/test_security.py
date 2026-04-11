"""Security and edge-case tests — run against a live local server with real DB.

Usage:
    docker-compose up db -d
    alembic upgrade head
    python main.py &
    pytest tests/test_security.py -v
"""

import json
import os
import uuid

import httpx
import pytest

BASE_URL = "http://localhost:8000"


@pytest.fixture
async def fresh_api_key():
    """Register a fresh user and return their API key."""
    email = f"test_security_{uuid.uuid4()}@example.com"
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post("/register", json={"email": email})
        assert resp.status_code == 200, f"Registration failed: {resp.text}"
        return resp.json()["api_key"]


# ─────────────────────────────────────────────
# Registration validation
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_invalid_email():
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post("/register", json={"email": "notanemail"})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_empty_email():
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post("/register", json={"email": ""})
        assert resp.status_code == 422


@pytest.mark.asyncio
async def test_register_rate_limit():
    """6th registration from same IP within an hour should return 429."""
    if os.environ.get("CI"):
        pytest.skip("Skipping rate limit test in CI")

    emails = [f"ratelimit_test_{uuid.uuid4()}@example.com" for _ in range(6)]
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        # First 5 should succeed
        for email in emails[:5]:
            resp = await client.post("/register", json={"email": email})
            if resp.status_code != 200:
                pytest.skip("First 5 registrations did not all succeed — skipping rate limit check")

        # 6th should be rate-limited
        resp = await client.post("/register", json={"email": emails[5]})
        assert resp.status_code == 429


# ─────────────────────────────────────────────
# Anti-enumeration
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resend_key_always_200():
    """Resending key for a nonexistent email should return 200 (no user enumeration)."""
    nonexistent = f"doesnotexist_{uuid.uuid4()}@example.com"
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post("/resend-key", json={"email": nonexistent})
        assert resp.status_code == 200


# ─────────────────────────────────────────────
# MCP tool edge cases
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_oversized_query(fresh_api_key):
    """A 10000-char query should be silently truncated, not crash."""
    api_key = await fresh_api_key
    big_query = "x" * 10000
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "search_opportunities_tool",
                    "arguments": {"api_key": api_key, "query": big_query, "limit": 1},
                },
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        result_text = body["result"]["content"][0]["text"]
        data = json.loads(result_text)
        assert isinstance(data, dict)
        assert "results" in data or "error" in data


@pytest.mark.asyncio
async def test_mcp_massive_limit(fresh_api_key):
    """limit: 9999 should be capped to 50 — response must have at most 50 results."""
    api_key = await fresh_api_key
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_opportunities_tool",
                    "arguments": {"api_key": api_key, "limit": 9999},
                },
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        result_text = body["result"]["content"][0]["text"]
        data = json.loads(result_text)
        if "results" in data:
            assert len(data["results"]) <= 50


@pytest.mark.asyncio
async def test_mcp_invalid_uuid(fresh_api_key):
    """A garbage opportunity_id should return an error dict, not crash."""
    api_key = await fresh_api_key
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_opportunity_details",
                    "arguments": {"api_key": api_key, "opportunity_id": "not-a-uuid-or-id"},
                },
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        result_text = body["result"]["content"][0]["text"]
        data = json.loads(result_text)
        assert "error" in data


@pytest.mark.asyncio
async def test_mcp_negative_offset(fresh_api_key):
    """A negative offset should not crash — returns a valid dict."""
    api_key = await fresh_api_key
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "search_opportunities_tool",
                    "arguments": {"api_key": api_key, "offset": -999},
                },
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        result_text = body["result"]["content"][0]["text"]
        data = json.loads(result_text)
        assert isinstance(data, dict)
        assert "results" in data or "error" in data


# ─────────────────────────────────────────────
# Webhook security
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_paddle_webhook_no_signature():
    """Paddle webhook with no signature header should return 401."""
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post(
            "/webhooks/paddle",
            json={"event_type": "subscription.created", "data": {}},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_paddle_webhook_invalid_signature():
    """Paddle webhook with a fake signature should return 401."""
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post(
            "/webhooks/paddle",
            content=b'{"event_type":"subscription.created","data":{}}',
            headers={
                "Content-Type": "application/json",
                "Paddle-Signature": "ts=1234567890;h1=fakehash",
            },
        )
        assert resp.status_code == 401
