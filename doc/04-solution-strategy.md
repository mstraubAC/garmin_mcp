# 4. Solution strategy

The five load-bearing decisions, each one in two paragraphs. Full
context + alternatives considered live in the matching ADR (chapter 9).

## 1. OAuth proxy in front of Entra ID — [ADR-001](09-architecture-decisions.md#adr-001-oauth-proxy-in-front-of-entra-id)

Claude apps need Dynamic Client Registration (RFC 7591) to register
themselves; Entra ID doesn't offer DCR. So we run a small OAuth 2.1
authorization server inside the MCP process that *speaks DCR to Claude*
(persists the client record locally) and *speaks pre-registered-client
OAuth to Entra* (one app registration shared by all Claude instances).

We issue our own short-lived JWT access tokens (HS256, 1 h TTL) so
clients never touch Entra tokens. The MCP resource-server middleware
verifies our JWT on every `/mcp` request and sets a per-request
`user_id` ContextVar that downstream tools read implicitly.

## 2. Streamable-HTTP transport, mounted in a Starlette wrapper — [ADR-002](09-architecture-decisions.md#adr-002-streamable-http-over-sse)

The MCP spec deprecated the SSE transport in 2026; streamable-HTTP is
the modern path. FastMCP gives us the `/mcp` endpoint; we mount it
under a Starlette app so we can add the OAuth, onboarding, and
healthcheck routes alongside it without forking FastMCP.

The lifespan composes the FastMCP session manager with our cleanup
loop (background asyncio task) and the Garmin client cache initialisation
— one process, one event loop, no separate cron container.

## 3. SQLite for all state — [ADR-003](09-architecture-decisions.md#adr-003-sqlite-over-postgres-for-state)

One file, one volume mount, zero ops. Eight tables: `oauth_clients`,
`pending_authorizations`, `oauth_codes`, `refresh_tokens`, `users`,
`garmin_tokens`, `rate_limit_buckets`, `schema_version`. WAL mode +
a single write lock keeps it safe for the small read/write loads a
personal-scale deployment generates.

We pay for this in scale ceiling — a few dozen active users on one
SQLite is the sane upper bound. When we hit it the migration to
Postgres is a `Storage`-class swap; everything else is unchanged.

## 4. Docker Compose, single VPS — [ADR-004](09-architecture-decisions.md#adr-004-docker-compose-over-k8s)

Two containers (`garmin-mcp` + `caddy`), three named volumes, one
compose file. Caddy fronts everything with auto-Let's-Encrypt; the MCP
container never binds to a public port. One person can deploy + back up
+ rotate secrets from an SSH session.

Trade-off: no HA, single VPS = SPOF. Acceptable for personal scale;
documented in [chapter 11](11-risks-and-technical-debt.md).

## 5. Per-user Garmin client cache, encrypted at rest — [ADR-005](09-architecture-decisions.md#adr-005-per-user-garmin-client-cache) + [ADR-006](09-architecture-decisions.md#adr-006-fernet-encrypted-garmin-tokens)

Each user's Garmin OAuth tokens are stored as a Fernet-encrypted blob
keyed by our internal `user_id`. The `MultiUserClientCache` lazy-loads
on demand, builds a `garth`-backed `Garmin` client, and keeps it in
memory with a 30-minute idle TTL.

Tools never see auth — they call `get_garmin_client()` which reads the
ContextVar set by the JWT verifier. Adding a tool means writing a
function, not understanding the OAuth flow.
