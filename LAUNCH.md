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
7. Set custom domain: `api.govconmcp.com` → Railway service URL
8. Verify: `curl https://api.govconmcp.com/health` → `{"status":"ok"}`

---

## Paddle Setup (Sandbox First)

1. Create account at paddle.com
2. Catalog → Products → Create 3:
   - "GovCon MCP Pro" — $49/mo recurring
   - "GovCon MCP Agency" — $199/mo recurring
3. Copy price IDs, add to Railway env: `PADDLE_PRO_PRICE_ID`, `PADDLE_AGENCY_PRICE_ID`
4. Notifications → Webhooks → Add endpoint: `https://api.govconmcp.com/webhooks/paddle`
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
- Use the SSE URL: `https://api.govconmcp.com/sse`

---

## Show HN Post

**Title:** Show HN: GovCon MCP – search federal contracts from Claude or Cursor

**Body:**
```
I built a paid MCP server for US government contracting intelligence.

France just shipped an official MCP server for data.gouv.fr (74K public datasets). The US
government's contracting data — $700B/year in federal spend — is sitting behind equally
unusable APIs. I built the MCP layer for it.

The problem: SAM.gov, Grants.gov, and SBIR each have different APIs, schemas, and quirks.
GovCon firms pay $500–2,000/month for tools like GovWin just to get structured access. There
was no MCP-native option.

What it does:
- Search across SAM.gov, Grants.gov, and SBIR with semantic + SQL hybrid search (pgvector +
  filters for NAICS, set-aside type, agency, value range, deadline)
- Parse solicitation PDFs — Claude Haiku extracts requirements, evaluation criteria, page limits,
  clearance requirements
- Score opportunities against your company profile (keyword overlap + NAICS match + set-aside
  eligibility + semantic similarity)
- Competitor intelligence via USASpending.gov — who wins what at which agencies
- Auto-match new opportunities to saved profiles every 15 minutes

Stack: FastMCP + FastAPI, PostgreSQL + pgvector, APScheduler, Claude Haiku (PDF parsing),
text-embedding-3-small (semantic search), Paddle billing, Railway.

Technical bits worth sharing:
- SAM.gov's description field often returns a URL placeholder instead of text, requiring a
  second authenticated API call with a daily quota that exhausts in minutes during backfill
- IVFFlat index needs SET LOCAL ivfflat.probes = 50 for small datasets — default probes=10
  with 100-list index returns empty results
- Match scoring uses two-pass candidate selection: NAICS-matched opps first, then pgvector
  nearest-neighbor fill. Without this, random DLA hardware parts pollute results for every profile
- SAM decommissioned ezSearch in early 2026 so v2 API docs are scattered and inconsistent

Free tier: 20 searches/day + 14-day Pro trial. Pro is $49/mo.

Would love feedback from anyone in GovCon or BD who's tried to automate opportunity pipeline work.

https://govconmcp.com
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

Free to try (20 searches/day + 14-day full access trial): govconmcp.com

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

Free to try: govconmcp.com

Would love feedback from BD professionals — what's the most painful part of
your opportunity pipeline?
```
