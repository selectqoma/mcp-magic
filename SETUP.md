# Manual Setup Checklist

Everything you need to fill in before the server runs. Check off as you go.

---

## 1. API Keys (copy .env.example → .env)

| Key | Where to get it | Status |
|-----|----------------|--------|
| `SAM_GOV_API_KEY` | [sam.gov/profile/details](https://sam.gov/profile/details) → API keys tab | ☐ |
| `OPENAI_API_KEY` | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | ☐ |
| `ANTHROPIC_API_KEY` | [console.anthropic.com/keys](https://console.anthropic.com/keys) | ☐ |
| `PADDLE_API_KEY` | [vendors.paddle.com/authentication](https://vendors.paddle.com/authentication) | ☐ (Week 3) |
| `PADDLE_WEBHOOK_SECRET` | Paddle dashboard → Notifications → Webhooks | ☐ (Week 3) |
| `RESEND_API_KEY` | [resend.com/api-keys](https://resend.com/api-keys) — verify `mcpmagic.io` domain first | ☐ |
| `SECRET_KEY` | Any long random string (`openssl rand -hex 32`) | ☐ |

---

## 2. Domain & Hosting

| Task | Notes | Status |
|------|-------|--------|
| Register `mcpmagic.io` (or similar) | Namecheap, Cloudflare Registrar | ☐ |
| Create Railway account | [railway.app](https://railway.app) | ☐ |
| Deploy Postgres on Railway | Add PostgreSQL plugin, enable pgvector extension | ☐ |
| Deploy server service on Railway | Point to this repo, set env vars | ☐ |
| Deploy worker service on Railway | Same repo, `startCommand = python worker.py` | ☐ |
| Set `DATABASE_URL` in Railway env | Copy from Railway Postgres plugin | ☐ |

---

## 3. Paddle Billing Setup (Week 3)

| Task | Notes | Status |
|------|-------|--------|
| Create Paddle account | [paddle.com](https://paddle.com) — use sandbox first | ☐ |
| Create 3 products: Free, Pro ($49), Agency ($199) | Paddle dashboard → Catalog | ☐ |
| Set `PADDLE_API_KEY` and `PADDLE_WEBHOOK_SECRET` | From Paddle dashboard | ☐ |
| Configure webhook URL | `https://your-domain.com/webhooks/paddle` | ☐ |
| Subscribe to events: `subscription.created`, `subscription.updated`, `subscription.canceled` | Paddle → Notifications | ☐ |

---

## 4. MCP Registries (Week 4 — at launch)

| Registry | URL | Status |
|----------|-----|--------|
| Smithery | [smithery.ai/new](https://smithery.ai/new) | ☐ |
| mcp.run | [mcp.run](https://mcp.run) | ☐ |
| PulseMCP | [pulsemcp.com/submit](https://pulsemcp.com/submit) | ☐ |

---

## 5. Launch Posts (Week 4)

| Channel | Status |
|---------|--------|
| Show HN: "Show HN: MCP server that searches 75K+ federal contract opportunities" | ☐ |
| r/govcon | ☐ |
| LinkedIn GovCon groups | ☐ |
| r/ClaudeAI, r/cursor | ☐ |

---

## Local Dev — First Run

```bash
cp .env.example .env
# Fill in SAM_GOV_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY

docker-compose up db -d          # start postgres
alembic upgrade head              # run migrations
python worker.py &                # start sync worker
python -m fastmcp run src/server.py --transport sse --port 8000
```

Test in Claude Desktop — add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "mcp-magic": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8000/sse"]
    }
  }
}
```
