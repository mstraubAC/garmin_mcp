# 9. Architecture decisions

ADRs use the [classic format](https://adr.github.io/madr/decisions/0000-use-markdown-architectural-decision-records.html):
context → decision → consequences → alternatives.

## ADR-001: OAuth proxy in front of Entra ID

**Status:** accepted (PR #5)

**Context.** Claude apps require [RFC 7591 Dynamic Client Registration](https://datatracker.ietf.org/doc/html/rfc7591)
to register themselves against an MCP server. Microsoft Entra ID does
not implement DCR — every OAuth client must be pre-registered as an
"app registration" with a known client_id. Pointing Claude clients
directly at Entra's `/authorize` endpoint therefore doesn't work; the
clients have nowhere to call `/register` against.

**Decision.** Run a small OAuth 2.1 authorization server inside the MCP
process (`auth/provider.py` implementing the MCP SDK's
`OAuthAuthorizationServerProvider`). It speaks **DCR + OAuth 2.1 to
Claude** (clients register dynamically; we persist them in SQLite) and
**pre-registered-client OAuth to Entra** (one app registration shared
by every Claude instance, secret stored on the VPS). It mints its own
short-lived JWT access tokens; clients never see Entra tokens.

**Consequences.**

- Adds an `auth/` package with ~7 modules, ~1500 lines.
- Adds operational responsibility for the JWT signing key and the
  encrypted-tokens key.
- Clean separation: tools never touch Entra or JWT details.
- Trade-off accepted: more code, in exchange for working integration
  with the Claude apps.

**Alternatives.**

- **Hosted IdP (Auth0 / WorkOS / Stytch)** — would solve the DCR mismatch
  by handling everything for us. Rejected: paid SaaS dependency we
  don't want, and it'd still need a per-user → Garmin mapping.
- **Cloudflare Workers OAuth Provider** — only works on Cloudflare.
  Out of scope: we want to self-host on a regular VPS.
- **Don't support Claude apps; only Claude Code** — Claude Code can
  inject custom headers, so a static Bearer would work. Rejected:
  defeats the "from anywhere" goal.

## ADR-002: Streamable-HTTP over SSE

**Status:** accepted (PR #3)

**Context.** The MCP spec offered two transports historically: SSE
(legacy) and Streamable-HTTP (since the 2025-06-18 revision). SSE is
deprecated as of 2026.

**Decision.** Use Streamable-HTTP. FastMCP's `streamable_http_app()`
gives us the `/mcp` endpoint; we mount it under a Starlette app so we
can co-locate the OAuth, onboarding, and healthcheck routes.

**Consequences.**

- No legacy SSE clients can connect; this is fine — Claude apps speak
  streamable-HTTP.
- Caddy needs `flush_interval=-1` so streamed responses aren't buffered.
- Lifespan composition becomes our responsibility (we manually run
  `mcp.session_manager.run()` inside the parent Starlette's lifespan).

## ADR-003: SQLite over Postgres for state

**Status:** accepted (PR #5)

**Context.** The OAuth proxy needs durable state for ~6 entities
(clients, codes, refresh tokens, users, garmin_tokens, rate limit
buckets). Total expected row counts at personal scale: low thousands.
Read/write rates: dozens per minute peak.

**Decision.** Use SQLite. WAL mode + a single `threading.Lock` around
writes for safety. One `Storage` class fronts every DB operation;
sync sqlite3 wrapped in `asyncio.to_thread()` at async call sites.

**Consequences.**

- One file = one volume mount = one backup target.
- No DB container, no DB user management, no schema migration tooling.
- Hard scale ceiling around ~dozens of active users and a few hundred
  registered Claude clients (write contention starts to hurt above that).
- The migration to Postgres later is a `Storage`-class swap; nothing
  else has to change because every query goes through that class.

**Alternatives.**

- **Postgres** in another container — extra moving piece for a single-user
  personal deployment; overkill for the scale we're targeting.
- **Filesystem (one JSON file per record)** — race conditions, no
  transactions, no querying. Worse on every axis.

## ADR-004: Docker Compose over Kubernetes

**Status:** accepted (PR #7)

**Context.** Single-host, single-operator deployment. No HA goal. No
CI/CD pipeline yet. Operator wants to be able to SSH in, look at logs,
restart things.

**Decision.** Two-service docker-compose stack. Caddy for TLS,
garmin-mcp for the application. Three named volumes. No orchestrator.

**Consequences.**

- Whole stack starts with `docker compose up -d --build`.
- Updates are `git pull && docker compose up -d --build`.
- No support for rolling deploys (tiny outage on every update).
- No support for multi-node HA (single VPS = SPOF — see
  [chapter 11](11-risks-and-technical-debt.md)).
- Container ordering handled via `depends_on: condition: service_healthy`,
  so Caddy doesn't try to proxy to garmin-mcp before it's up.

## ADR-005: Per-user Garmin client cache, in-memory

**Status:** accepted (PR #6)

**Context.** Each authenticated user needs a `garth`-backed `Garmin`
client built from their stored tokens. Building one involves
`garth.loads(blob)` — not free; a few ms. A single Claude session may
make 10+ tool calls over a minute. Building a fresh client per call is
wasteful.

**Decision.** `MultiUserClientCache` keeps fully-built `Garmin`
instances in memory keyed by user_id, with a 30-minute idle TTL.
Cache misses decrypt the user's blob and build a new instance. There
is no upper bound on the cache size; the idle TTL bounds memory in
practice.

**Consequences.**

- No first-call latency for the second-onwards tool call in a session.
- A long-idle user's instance is reclaimed without explicit eviction
  code.
- The cache lives in process memory — restarts drop it (acceptable;
  rebuild cost is ms).
- A real problem: stale `garth` sessions when Garmin tokens expire (~6
  months). Currently the cache will return a stale client and the next
  tool call will get a 401 from Garmin. See
  [chapter 11 — Garmin token refresh detection](11-risks-and-technical-debt.md).

**Alternatives.**

- **No cache, build per request** — adds 1-5 ms latency to every tool
  call; not catastrophic but pointless.
- **LRU with size cap** — adds complexity for no benefit at our scale;
  the idle TTL achieves the same goal.

## ADR-006: Garmin tokens encrypted with Fernet, key in env var

**Status:** accepted (PR #6)

**Context.** Garmin OAuth tokens give access to a real user's Garmin
account. We persist them so users don't have to re-enter their password
every time their session expires (currently never, until garth's tokens
themselves expire after ~6 months). They MUST be encrypted at rest —
DB read access shouldn't equal Garmin account compromise.

**Decision.** Wrap each token blob with [Fernet](https://cryptography.io/en/latest/fernet/)
(AES-128-CBC + HMAC-SHA256, well-tested, hard to misuse). Key comes from
the `GARMIN_MCP_DATA_KEY` env var; the env file lives at
`/etc/garmin-mcp/env` mode 0600 owned by root.

**Consequences.**

- DB compromise alone doesn't yield Garmin tokens.
- The key MUST be backed up — losing it makes every stored blob
  unreadable, every user has to re-onboard.
- Rotation requires a "decrypt with old key, re-encrypt with new key"
  migration. Currently not implemented; deferred to step 9.

**Alternatives.**

- **OS keyring (macOS Keychain, Linux Secret Service)** — not available
  in the headless container context.
- **Vault / SOPS / etc.** — adds another moving piece; overkill for
  one operator.
- **Don't persist the password, re-prompt on token expiry** — that's
  exactly what we do; the encrypted blob is *not* the password.
