# 8. Crosscutting concepts

## Authentication and authorization

Two layers, both involving JWTs but for different purposes:

| Layer | Issuer | Verifier | Lifetime | Audience |
|---|---|---|---|---|
| **Entra ID id_token** | Entra | `auth/entra.py` (RS256, JWKS) | ~1 h | Our app's `client_id` |
| **MCP access token** | `auth/jwt.py` (HS256) | `auth/jwt.py` | 1 h (default) | `<MCP_PUBLIC_URL>/mcp` |

Authorization is intentionally minimal: a single `mcp.use` scope,
required for every `/mcp` request. Tool-level scoping is out of scope
(every authenticated user can call every tool — but only against
*their own* Garmin data, see "multi-user isolation" below).

## Encryption at rest

Only the Garmin OAuth tokens are encrypted at rest. The encryption is
[Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256)
with the key from `GARMIN_MCP_DATA_KEY`. The MCP access tokens are
stateless JWTs (no need to encrypt — they're verifiable by signature
alone), and refresh tokens are stored as SHA-256 hashes (not the token
itself), so a DB read can't recover them.

What's *not* encrypted at rest: OAuth client metadata, audit log,
SQLite schema. None of those carry secrets.

## Multi-user isolation

Three independent enforcement points; any two failing still leaves the
third as backstop.

1. **Token verification sets the user_id** —
   `provider.load_access_token` calls `set_current_user_id(claims.user_id)`
   inside the JWT verifier. A request can never *not* have a user_id by
   the time tools run.
2. **The Garmin client cache is keyed by user_id** —
   `MultiUserClientCache.get_or_load(user_id)` calls
   `token_store.load(user_id)` and decrypts that user's blob. There is
   no path that hands back another user's instance.
3. **Tools never accept a user_id parameter** — they call
   `get_garmin_client()` with no arguments. A tool can't be tricked
   into operating on the wrong user via a JSON-RPC parameter.

## Logging

Two streams:

- **Application log** (Python `logging`, default `INFO`) — goes to
  stdout, captured by Docker. Standard ops + diagnostics.
- **Audit log** (`auth/audit.py`) — one JSON line per OAuth event
  (`register.success`, `authorize.start`, `token.issued`, …) in
  `/var/log/garmin-mcp/audit-YYYY-MM-DD.log`. Failures to write are
  swallowed — auditing must never break a request.

## Rate limiting

Defense in depth around the publicly-reachable endpoints:

- **Per-IP token bucket on `/register`** — `RegistrationGuard` uses a
  `TokenBucket` (5 successful registrations / IP / hour). Persisted in
  SQLite so an attacker restart-bot can't reset the counter.
- **Global cap on `oauth_clients`** — refuse new registrations when the
  table exceeds 10,000 rows.
- **TTL on unused / idle clients** — background cleanup loop drops rows
  that registered but never exchanged a token (>24 h) or haven't been
  used in 90 days.
- **Optional shared lockdown token** — when `MCP_REGISTRATION_TOKEN` is
  set, `/register` requires it as a Bearer header; useful while the
  deploy is still being tested.

There's *no* per-user rate limit on tool calls (yet). The single
outbound IP shared by everyone makes this a real risk that's deferred
to step 9 (hardening). See [chapter 11](11-risks-and-technical-debt.md).

## Error handling

Three categories with distinct shapes:

| Class | Example | Surface |
|---|---|---|
| **Expected user errors** | bad MFA, invalid date format | Translated to friendly text in the response (HTML for /onboard, JSON-RPC error for tools) |
| **Auth failures** | bad signature, expired token | 401 + WWW-Authenticate; client should refresh |
| **Internal errors** | DB locked, Entra unreachable | 5xx + audit-logged; user sees a generic message |

The provider raises `mcp.server.auth.provider.TokenError` /
`RegistrationError` for protocol-level errors, which FastMCP converts
to the right RFC 6749 JSON shape automatically.

## Concurrency model

- One uvicorn worker, single asyncio event loop, no multiprocessing.
- `asyncio.to_thread()` wraps every blocking storage call so the loop
  stays responsive under load.
- The onboarding worker runs in a daemon `threading.Thread` because
  `garth.login()` is purely synchronous; it communicates back to the
  async layer via a `threading.Event` + `queue.Queue` pair.
- The MCP session manager uses anyio task groups internally; we just
  use its lifespan context.

## Schema evolution

Rules:

1. New tables only; never drop or alter existing columns.
2. Bump `SCHEMA_VERSION` in `auth/storage.py` for tracking, but the DDL
   is `CREATE TABLE IF NOT EXISTS` so the same DDL runs on fresh and
   upgraded DBs.
3. The init code accepts a *lower* version (forward upgrade), refuses a
   *higher* version (the binary is too old for the DB).

## Configuration

All runtime config flows through env vars (see `deploy/env.example`).
No config files; nothing to YAML-parse on startup; one source of truth
that maps cleanly to `docker-compose.yml`'s `env_file:`.

Required vars are read with `os.environ[...]` (KeyError = fail fast at
startup); optional vars use `os.environ.get(...)` with defaults.
