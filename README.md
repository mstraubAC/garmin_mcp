# Garmin MCP server — multi-user fork

A Model Context Protocol (MCP) server that exposes Garmin Connect data
(activities, sleep, training, nutrition, workouts, devices, …) as tools
that any MCP client — Claude Desktop, Claude apps, MCP Inspector — can
call.

This is a fork of [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp).
The upstream README has detailed background on the tool surface and the
underlying [python-garminconnect](https://github.com/cyberjunky/python-garminconnect)
library; this README documents the additions in this fork and how to get
running quickly.

## What this fork adds

| Capability | Upstream | This fork |
|---|---|---|
| stdio MCP transport (Claude Desktop) | ✅ | ✅ |
| Remote HTTP transport reachable from Claude apps | — | ✅ |
| Multi-user — each authenticated user → their own Garmin account | — | ✅ |
| Microsoft Entra ID sign-in via OAuth 2.1 + DCR | — | ✅ |
| Per-user Garmin tokens encrypted at rest (Fernet) | — | ✅ |
| One-time onboarding with full MFA support | — | ✅ |
| Docker Compose stack with Caddy + auto-TLS | — | ✅ |
| Azure infrastructure as Bicep (`infra/azure/`) | — | ✅ |
| arc42 architecture docs (`doc/`) | — | ✅ |
| 320 automated tests | partial | ✅ |

The MCP tool surface (96+ tools across activities, health, training,
workouts, nutrition, …) is inherited unchanged from upstream.

## Operating modes

Pick whichever matches your setup:

- **Local stdio** — runs on your laptop, talks to a single Garmin account
  via env vars. Use with Claude Desktop / Claude Code. Same shape as
  upstream.
- **Local HTTP** — runs as a uvicorn process on `127.0.0.1:8000`. Use with
  the MCP Inspector or for development without going through Docker.
- **Public HTTP, multi-user** — runs behind Caddy on a VPS, gates access
  with Microsoft Entra ID. Each user goes through a one-time onboarding
  to connect their own Garmin account. Use from the Claude apps
  (mobile, web, desktop) over the internet.

## Quick start

### Prerequisites

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) for local dev
- Docker + Docker Compose v2 — only for the test deployment

### Local: stdio mode

```bash
uv sync

# One-time interactive auth — stores OAuth tokens in ~/.garminconnect
uv run garmin-mcp-auth

# Add to Claude Desktop config (~/Library/Application Support/Claude/claude_desktop_config.json)
# Replace <repo path> with your absolute path:
```

```json
{
  "mcpServers": {
    "garmin-local": {
      "command": "uv",
      "args": ["--directory", "<repo path>", "run", "garmin-mcp"]
    }
  }
}
```

Restart Claude Desktop and your Garmin tools are available.

### Local: HTTP mode (no auth, dev only)

```bash
uv sync
export GARMIN_EMAIL=you@example.com
export GARMIN_PASSWORD=...

# Drive the no-auth single-user app directly via uvicorn.
uv run uvicorn garmin_mcp.server:app --host 127.0.0.1 --port 8000
```

```bash
# In another shell, verify with the MCP Inspector at http://127.0.0.1:8000/mcp
npx @modelcontextprotocol/inspector
```

This is the same FastMCP server over HTTP, against a single Garmin
account from env vars. Auth is **not** enforced — the path exists for
local development against the MCP Inspector. Bind to `127.0.0.1` and
don't expose the port.

The `garmin-mcp-http` script wires the *production* OAuth-protected
app and requires the full set of env vars from
[`deploy/env.example`](deploy/env.example) — see the next section.

### Test deployment via Docker Compose

For trying the full multi-user OAuth-protected stack on a single host
(VPS, dev box, doesn't matter):

1. **Provision the Entra app registration** — see [`infra/azure/README.md`](infra/azure/README.md).
   `./scripts/deploy.sh prod` prints the env snippet you'll paste below.
2. **Bootstrap the host** — see [`deploy/README.md`](deploy/README.md) for the
   full walkthrough (DNS, env file at `/etc/garmin-mcp/env`, secrets,
   Caddy hostname). The condensed version:

   ```bash
   sudo install -d -m 700 /etc/garmin-mcp
   sudo $EDITOR /etc/garmin-mcp/env       # paste the snippet from infra/azure
   sudo chmod 600 /etc/garmin-mcp/env

   cd deploy/
   docker compose up -d --build
   ```

3. Browse to `https://<your-hostname>/healthz` — should return
   `{"status":"ok"}`.
4. Add the connector in a Claude app: paste
   `https://<your-hostname>/mcp` as a custom MCP server and walk through
   the Entra sign-in + Garmin onboarding flow.

The four runtime flows (DCR, OAuth, onboarding, tool call) are diagrammed
in [`doc/06-runtime-view.md`](doc/06-runtime-view.md).

## Running tests

```bash
uv sync
uv run pytest                                   # full suite (~320 tests)
uv run pytest tests/unit/                       # unit only
uv run pytest tests/integration/ -v             # integration only
uv run pytest tests/integration/test_oauth_flow.py     # OAuth end-to-end
```

The e2e tests under `tests/e2e/` need real Garmin credentials and are
skipped by default. Run with `pytest -m e2e`.

## Project layout

```
src/garmin_mcp/
  __init__.py           stdio entry point + Garmin client init
  server.py             ASGI app + make_app/make_production_app factories
  user_context.py       per-request user lookup (single + multi-user caches)
  <12 tool modules>.py  activities, health, workouts, nutrition, …
  auth/                 OAuth proxy + onboarding (only used in HTTP mode)
  maintenance/          background TTL cleanup
tests/
  unit/                 pure unit tests
  integration/          FastMCP integration tests + OAuth + onboarding flows
  e2e/                  real-Garmin tests (opt in with -m e2e)
deploy/                 Dockerfile, docker-compose, Caddyfile, env.example
infra/azure/            Bicep + scripts for the Entra app registration
doc/                    arc42 architecture documentation (12 chapters + diagrams)
```

## Documentation

The arc42 docs in [`doc/`](doc/) are the source of truth for everything
beyond "how do I run this":

- [`doc/04-solution-strategy.md`](doc/04-solution-strategy.md) — the load-bearing decisions in one page
- [`doc/06-runtime-view.md`](doc/06-runtime-view.md) — sequence diagrams for the four critical flows
- [`doc/07-deployment-view.md`](doc/07-deployment-view.md) — what runs where, volumes, secrets
- [`doc/09-architecture-decisions.md`](doc/09-architecture-decisions.md) — the ADRs with full context
- [`doc/11-risks-and-technical-debt.md`](doc/11-risks-and-technical-debt.md) — what we know is fragile

Operational walkthroughs:

- [`deploy/README.md`](deploy/README.md) — VPS bootstrap + day-2 ops + troubleshooting
- [`infra/azure/README.md`](infra/azure/README.md) — Entra app registration + secret rotation

## Contributing

### Definition of Done

A change is **not done** until both the code and the docs are updated to
match. Concretely, before opening a PR:

1. **Code changes ship with tests.** New behavior gets a test; bug fixes
   get a regression test.
2. **`doc/` is updated** when the change touches anything covered by the
   arc42 chapters:
   - New module under `src/garmin_mcp/` → update [`doc/05-building-block-view.md`](doc/05-building-block-view.md)
   - New auth or onboarding step → update [`doc/06-runtime-view.md`](doc/06-runtime-view.md)
   - New env var or container mount → update [`doc/07-deployment-view.md`](doc/07-deployment-view.md)
   - Load-bearing design choice (would touch ≥3 files to flip) → add an ADR in [`doc/09-architecture-decisions.md`](doc/09-architecture-decisions.md)
   - Newly-discovered fragility → log it in [`doc/11-risks-and-technical-debt.md`](doc/11-risks-and-technical-debt.md)
3. **`README.md` is updated** when the surface a new contributor first
   sees changes — operating modes, prerequisites, project layout, or any
   command in the Quick Start sections.

### For AI assistants (Claude, GitHub Copilot, …)

The same Definition of Done applies. Treat documentation as part of the
diff, not a follow-up. Tooling-readable copies of these conventions live
in [`AGENTS.md`](AGENTS.md) (cross-tool standard) and
[`.github/copilot-instructions.md`](.github/copilot-instructions.md).

## Credits

- [Taxuspt/garmin_mcp](https://github.com/Taxuspt/garmin_mcp) — the
  upstream this fork extends; all of the MCP tool implementations come
  from there.
- [`python-garminconnect`](https://github.com/cyberjunky/python-garminconnect)
  and [`garth`](https://github.com/matin/garth) — the Garmin Connect
  client libraries everything sits on.
- [Model Context Protocol](https://modelcontextprotocol.io) — the
  specification this server implements.
