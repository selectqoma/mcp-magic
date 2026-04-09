# Launch Playbook

Copy-paste ready posts and deploy steps for launch day.

---

## Deploy to Railway

1. Push this repo to GitHub (done ✓)
2. Go to railway.app → New Project → Deploy from GitHub repo → select `mcp-magic`
3. Add PostgreSQL plugin → enable pgvector:
   ```sql
   CREATE EXTENSION IF NOT EXISTS vector;
   ```
4. Set environment variables (from SETUP.md checklist):
   - `DATABASE_URL` (copy from Railway Postgres plugin)
   - `SAM_GOV_API_KEY`
   - `OPENAI_API_KEY`
   - `ANTHROPIC_API_KEY`
   - `PADDLE_API_KEY`
   - `PADDLE_WEBHOOK_SECRET`
   - `SECRET_KEY`
5. Add a second service (worker):
   - Same repo, override start command: `python worker.py`
   - Same env vars
6. Run migration (Railway shell or local with Railway DB URL):
   ```bash
   alembic upgrade head
   ```
7. Set custom domain: `api.mcpmagic.io` → Railway service URL
8. Verify: `curl https://api.mcpmagic.io/health` → `{"status":"ok"}`

---

## Paddle Setup (Sandbox First)

1. Create account at paddle.com
2. Catalog → Products → Create 3:
   - "MCP Magic Pro" — $49/mo recurring
   - "MCP Magic Agency" — $199/mo recurring
3. Copy price IDs, add to Railway env: `PADDLE_PRO_PRICE_ID`, `PADDLE_AGENCY_PRICE_ID`
4. Notifications → Webhooks → Add endpoint: `https://api.mcpmagic.io/webhooks/paddle`
5. Subscribe to: `subscription.created`, `subscription.updated`, `subscription.canceled`, `subscription.past_due`
6. Copy webhook secret → `PADDLE_WEBHOOK_SECRET`
7. Test with Paddle sandbox before going live

---

## MCP Registry Listings

### Smithery
1. Go to smithery.ai → Submit Server
2. Link to this repo — `smithery.yaml` is already configured
3. Verify the listing pulls the correct tools and description

### PulseMCP
- Submit at pulsemcp.com/submit
- Category: Business / Government
- Pricing: Freemium

### mcp.run
- Submit at mcp.run → add listing
- Use the SSE URL: `https://api.mcpmagic.io/sse`

---

## Show HN Post

**Title:** Show HN: MCP server that searches 75K+ federal contract opportunities

**Body:**
```
I built a paid MCP server for government contract intelligence.

The problem: SAM.gov has 75,000+ active federal opportunities, but their UI and API are terrible.
GovCon firms pay $500-2,000/month for tools like GovWin just to get structured access. There
was no MCP-native option.

What it does:
- Search SAM.gov, Grants.gov, and SBIR.gov with semantic + SQL hybrid search (NAICS, set-aside,
  agency, value, deadline filters)
- Parse solicitation PDFs with Claude Haiku — extracts requirements, eval criteria, page limits,
  clearance requirements
- Score opportunities against your company profile (keyword + NAICS + set-aside eligibility +
  semantic similarity)
- Track competitor award history via USASpending.gov
- Auto-match new opportunities to saved profiles every 15 minutes

Stack: FastMCP + FastAPI, PostgreSQL + pgvector, APScheduler, Claude Haiku for PDF parsing,
text-embedding-3-small for semantic search. Deployed on Railway.

Free tier: 20 searches/day + 14-day Pro trial. Pro is $49/mo.

The interesting technical bit: SAM.gov decommissioned their ezSearch API in Feb 2026, so the
docs are a mess. The semantic search is genuinely useful because contract titles like
"J045—MEDICAL EQUIPMENT MAINTENANCE SVCS" don't surface with keyword search for
"hospital equipment maintenance."

Would appreciate any feedback from people who work in GovCon or have tried building with MCP.

Link: mcpmagic.io
```

---

## r/govcon Post

**Title:** Built an AI agent tool for finding federal contracts — feedback welcome

**Body:**
```
Hey r/govcon,

I've been building a tool that lets AI agents (Claude, Cursor, etc.) search and analyze
federal opportunities directly. Thought this community would have useful feedback.

What it does:
- Real-time search across SAM.gov, Grants.gov, SBIR
- Parses solicitation PDFs to extract requirements and eval criteria
- Scores opportunities against your company's capabilities and certifications
- Tracks competitor award history (USASpending)
- Sends notifications when new matches appear for your profile

The goal was to make it feel like asking a colleague "find me active SDVOSB cybersecurity
contracts at DoD above $500K" instead of clicking through SAM's interface.

Free to try (20 searches/day + 14-day full access trial): mcpmagic.io

Honest question: what would make this actually useful for your BD process? What do you spend
the most time on that could be automated?
```

---

## LinkedIn Post (GovCon Groups)

**Groups to post in:**
- Government Contractors Network (500K+ members)
- Federal Contracting Professionals
- Small Business Government Contracting

**Post:**
```
Spent the last month building something for the GovCon community.

The pain: SAM.gov has 75,000+ active opportunities. The interface is painful.
The existing AI tools don't search it properly.

So I built an MCP server — a tool that lets AI agents (Claude, Cursor) query
federal contracts intelligently:

✓ Semantic search across SAM, Grants.gov, SBIR
✓ PDF parsing — requirements, eval criteria, page limits extracted automatically
✓ Scores opportunities against your NAICS codes + certifications
✓ Competitor intelligence via USASpending

One question to get started: "Find me active SDVOSB cybersecurity contracts
at DoD above $500K, deadline in 30 days"

Free to try: mcpmagic.io

Would love feedback from BD professionals — what's the most painful part of
your opportunity pipeline?
```
