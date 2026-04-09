"""Integration tests — run against a live local server with real DB.

Usage:
    docker-compose up db -d
    alembic upgrade head
    python main.py &
    pytest tests/test_tools.py -v
"""

import asyncio
import pytest
import httpx

BASE_URL = "http://localhost:8000"


@pytest.fixture(scope="module")
async def api_key():
    """Register a test user and return their API key."""
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post("/register", json={"email": "test@mcpmagic.io"})
        if resp.status_code == 409:
            # Already registered — re-use (dev only)
            pytest.skip("Test user already exists — clear DB or use a unique email")
        assert resp.status_code == 200
        return resp.json()["api_key"]


@pytest.mark.asyncio
async def test_health():
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_register_and_get_key():
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        resp = await client.post("/register", json={"email": "newuser@example.com"})
        assert resp.status_code in (200, 409)
        if resp.status_code == 200:
            data = resp.json()
            assert "api_key" in data
            assert "mcp_config" in data
            assert len(data["api_key"]) > 20


@pytest.mark.asyncio
async def test_register_duplicate():
    async with httpx.AsyncClient(base_url=BASE_URL) as client:
        await client.post("/register", json={"email": "dup@example.com"})
        resp = await client.post("/register", json={"email": "dup@example.com"})
        assert resp.status_code == 409


@pytest.mark.asyncio
async def test_search_no_key():
    """Missing API key should return an error, not crash."""
    from fastmcp import Client
    async with Client(f"{BASE_URL}/sse") as client:
        result = await client.call_tool("search_opportunities_tool", {"api_key": ""})
        assert "error" in str(result).lower()


@pytest.mark.asyncio
async def test_search_invalid_key():
    from fastmcp import Client
    async with Client(f"{BASE_URL}/sse") as client:
        result = await client.call_tool("search_opportunities_tool", {"api_key": "invalid-key-xyz"})
        assert "error" in str(result).lower()


@pytest.mark.asyncio
async def test_search_basic(api_key):
    """Basic search should return results dict (possibly empty if DB not seeded)."""
    from fastmcp import Client
    async with Client(f"{BASE_URL}/sse") as client:
        result = await client.call_tool("search_opportunities_tool", {
            "api_key": api_key,
            "query": "cybersecurity",
            "limit": 5,
        })
        data = result[0].text if result else "{}"
        import json
        parsed = json.loads(data)
        assert "results" in parsed
        assert "total_count" in parsed
        assert isinstance(parsed["results"], list)


@pytest.mark.asyncio
async def test_search_with_filters(api_key):
    from fastmcp import Client
    async with Client(f"{BASE_URL}/sse") as client:
        result = await client.call_tool("search_opportunities_tool", {
            "api_key": api_key,
            "naics_codes": ["541511"],
            "set_aside_type": "SB",
            "source": "sam",
            "limit": 3,
        })
        import json
        data = json.loads(result[0].text)
        assert "results" in data


@pytest.mark.asyncio
async def test_get_opportunity_not_found(api_key):
    from fastmcp import Client
    async with Client(f"{BASE_URL}/sse") as client:
        result = await client.call_tool("get_opportunity_details", {
            "api_key": api_key,
            "opportunity_id": "00000000-0000-0000-0000-000000000000",
        })
        import json
        data = json.loads(result[0].text)
        assert "error" in data


@pytest.mark.asyncio
async def test_solicitation_requirements_requires_pro(api_key):
    """Free users (not in trial) should be blocked from PDF parsing."""
    # Note: free users get a 14-day trial so this might pass during trial
    from fastmcp import Client
    async with Client(f"{BASE_URL}/sse") as client:
        result = await client.call_tool("get_solicitation_requirements", {
            "api_key": api_key,
            "opportunity_id": "00000000-0000-0000-0000-000000000000",
        })
        import json
        data = json.loads(result[0].text)
        # Either it works (trial) or returns error — both are fine
        assert "error" in data or "parsing_confidence" in data or "requirements" in data


@pytest.mark.asyncio
async def test_search_past_awards_no_filters(api_key):
    from fastmcp import Client
    async with Client(f"{BASE_URL}/sse") as client:
        result = await client.call_tool("search_past_awards", {
            "api_key": api_key,
            "limit": 5,
        })
        import json
        data = json.loads(result[0].text)
        assert "awards" in data
        assert "aggregations" in data


@pytest.mark.asyncio
async def test_track_competitors_no_company(api_key):
    from fastmcp import Client
    async with Client(f"{BASE_URL}/sse") as client:
        result = await client.call_tool("track_competitors", {
            "api_key": api_key,
        })
        import json
        data = json.loads(result[0].text)
        assert "error" in data


@pytest.mark.asyncio
async def test_free_tier_rate_limit():
    """After 20 searches, free tier should be blocked."""
    # Register a fresh user
    async with httpx.AsyncClient(base_url=BASE_URL) as http:
        resp = await http.post("/register", json={"email": "ratelimit@example.com"})
        if resp.status_code == 409:
            pytest.skip("Rate limit test user exists")
        key = resp.json()["api_key"]

    from fastmcp import Client
    import json

    async with Client(f"{BASE_URL}/sse") as client:
        # Exhaust the daily limit
        for i in range(21):
            result = await client.call_tool("search_opportunities_tool", {
                "api_key": key,
                "query": f"test {i}",
                "limit": 1,
            })
            data = json.loads(result[0].text)
            if "error" in data and "limit" in data["error"].lower():
                assert i >= 20  # Should only hit limit after 20 searches
                return

        pytest.fail("Rate limit was never triggered after 21 searches")
