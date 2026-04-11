# GovCon MCP — Government Contracts & Grants Intelligence

Search 75,000+ US federal opportunities from SAM.gov, Grants.gov, and SBIR.gov — directly from your AI agent.

## Quick Start (Claude Desktop)

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "govcon-mcp": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://api.govconmcp.com/sse"],
      "env": {
        "MCP_API_KEY": "your-api-key-here"
      }
    }
  }
}
```

Get a free API key at **govconmcp.com**.

## Tools

| Tool | Description | Tier |
|------|-------------|------|
| `search_opportunities_tool` | Search contracts & grants with semantic + filter search | Free (20/day) |
| `get_opportunity_details` | Full details + attachment list | Free |
| `get_solicitation_requirements` | Parse PDF requirements, eval criteria, deadlines | Pro |
| `match_to_profile` | Score opportunities against your company profile | Pro |
| `track_competitors` | Company award history from USASpending | Pro |
| `search_past_awards` | Market intelligence on federal awards | Pro |
| `get_notifications` | New matches for saved profiles | Pro |

## Development

```bash
cp .env.example .env
# Fill in API keys

docker-compose up db
alembic upgrade head
python worker.py &
python -m fastmcp run src/server.py --transport sse --port 8000
```

## Pricing

- **Free**: 20 searches/day
- **Pro** ($49/mo): Unlimited + PDF parsing + competitor tracking + 3 profiles
- **Agency** ($199/mo): Everything + 20 profiles + webhook alerts
