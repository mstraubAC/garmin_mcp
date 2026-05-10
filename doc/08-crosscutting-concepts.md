# 8. Crosscutting concepts

This chapter is **the durable security specification** for the
running system. Each section describes both *what we do today* and
*what MUST hold* (RFC 2119 keywords). When implementation drifts
from a requirement, that gap belongs in
[chapter 11](11-risks-and-technical-debt.md) until closed; the
requirement here doesn't move.

## Security requirements at a glance

| Concern | Section | Summary |
|---|---|---|
| Data sensitivity & privacy | [§ Data sensitivity](#data-sensitivity-and-privacy) | Personal health data (Garmin). Medical-application diligence required for correctness and privacy. |
| Authentication & authorization | [§ AuthN/Z](#authentication-and-authorization) | OAuth 2.1 + DCR + PKCE. JWT access tokens with rotatable signing key; refresh tokens rotated on use. |
| Encryption at rest | [§ Encryption at rest](#encryption-at-rest) | Garmin OAuth tokens Fernet-encrypted. Encryption keys rotatable. |
| Multi-user isolation | [§ Multi-user isolation](#multi-user-isolation) | Three independent enforcement points; user_id resolved before tool dispatch. |
| Web security headers | [§ Web security headers](#web-security-headers) | HSTS / CSP / X-Frame-Options / X-Content-Type-Options on every browser-facing response. |
| CSRF protection | [§ CSRF protection](#csrf-protection) | State-changing browser endpoints carry a per-session CSRF token. |
| Trust boundary | [§ Trust boundary](#trust-boundary) | The Caddy → uvicorn link is the only place X-Forwarded-* is trusted. |
| Logging & audit | [§ Logging](#logging) | Audit log hash-chained (tamper-evident); never carries secrets. |
| Rate limiting | [§ Rate limiting](#rate-limiting) | Per-IP DCR limit, per-user tool-call limit, global outbound limit. |
| Input validation | [§ Error handling and input validation](#error-handling-and-input-validation) | Length-bounded user input; uniform error shapes that don't enumerate users. |
| Concurrency safety | [§ Concurrency model](#concurrency-model) | Bounded session memory; bounded worker thread lifetime; race-free cache + user-row creation. |
| Schema evolution | [§ Schema evolution](#schema-evolution) | Forward-compat-only; single-writer process. |
| Configuration & secrets | [§ Configuration](#configuration) | Secrets only via env / mounted files; no defaults for required secrets. |
| Container hardening | [§ Container hardening](#container-hardening) | Non-root, read-only rootfs, dropped capabilities, resource limits. |
| Supply chain integrity | [§ Supply chain integrity](#supply-chain-integrity) | Base images pinned by digest; lock file checked in CI; CVE scan on every PR. |
| Operational health | [§ Operational health](#operational-health) | `/healthz` verifies the database; admin APIs disabled. |

---

## Data sensitivity and privacy

**The system handles personal health and fitness data.** Garmin Connect
data includes activities, sleep metrics, heart rate, weight, training
load, nutrition logs, and women's health information. While this
application is not a regulated medical device, it MUST be built and
maintained with equivalent diligence.

### Requirements

- **No silent data corruption.** Every tool that reads or writes health
  metrics MUST have test coverage for correctness. A bug that silently
  returns wrong sleep hours, heart rate, or training metrics could
  mislead the user's health decisions.
- **No cross-user data leakage.** Per-user isolation is the top
  architectural quality goal (see § 1). The `ContextVar` resolver,
  `MultiUserClientCache`, and Fernet encryption are load-bearing for
  this requirement.
- **Encryption at rest for all stored Garmin tokens.** The
  `garmin_tokens` SQLite column contains Fernet-encrypted OAuth tokens
  (see [§ Encryption at rest](#encryption-at-rest)). Plaintext tokens
  MUST never appear in logs, audit files, or error messages.
- **No logging of personal data.** Audit logs record authentication
  events (register, token issue) but MUST NOT contain Garmin data
  (activity names, health metrics). Application logs (`logging`) MUST
  NOT log raw API responses or user-specific health data.
- **Data portability.** Users own their data. The Garmin API already
  provides this; this server adds a per-user token layer that a user
  can revoke. There is no server-side data aggregation or analytics.
- **Accessibility.** Users can delete their Garmin token blob at any
  time via `/onboard` re-authentication, effectively removing stored
  credentials from the server.

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

### Requirements

- The MCP access token MUST be signed with a server-side secret that
  is rotatable without all currently-issued tokens becoming invalid;
  the implementation MUST support a `kid` (key ID) header so a
  previous key can verify until the natural expiry of tokens it
  signed.
- Access-token lifetime MUST be ≤ 1 hour.
- PKCE (RFC 7636, `S256`) MUST be required on every authorization
  code flow; the storage schema enforces this via `code_challenge
  TEXT NOT NULL` on `pending_authorizations` and `oauth_codes`.
- Refresh tokens MUST rotate on use: the old token is revoked at the
  same moment the new pair is issued, so reuse of a stolen refresh
  token is detectable.
- Refresh tokens MUST be stored as SHA-256 hashes, never plaintext.
- Dynamic Client Registration MUST allowlist `grant_types` to
  `{authorization_code, refresh_token}` and `response_types` to
  `{code}`. Clients requesting `password`, `implicit`, or other
  legacy flows MUST be rejected at registration.
- The Entra ID `tid` claim on every received `id_token` MUST be
  checked against the configured tenant ID; tokens from any other
  tenant MUST be rejected.

## Encryption at rest

Only the Garmin OAuth tokens are encrypted at rest. The encryption is
[Fernet](https://cryptography.io/en/latest/fernet/) (AES-128-CBC + HMAC-SHA256)
with the key from `GARMIN_MCP_DATA_KEY`. The MCP access tokens are
stateless JWTs (no need to encrypt — they're verifiable by signature
alone), and refresh tokens are stored as SHA-256 hashes (not the token
itself), so a DB read can't recover them.

What's *not* encrypted at rest: OAuth client metadata, audit log,
SQLite schema. None of those carry secrets.

### Requirements

- Garmin OAuth tokens MUST be encrypted at rest with an authenticated
  cipher (Fernet meets this).
- Both encryption keys (`GARMIN_MCP_DATA_KEY` for Garmin tokens,
  `JWT_SIGNING_KEY` for access tokens) MUST be rotatable via an
  operator workflow that doesn't lose data or invalidate every
  in-flight session.
- Garmin user passwords MUST NOT be persisted at any point. The
  password is held in memory for the duration of `garth.login()` and
  zeroed afterward; only the resulting OAuth tokens are stored.

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

### Requirements

- Every authenticated request MUST resolve to exactly one `user_id`
  before any tool dispatch; a request that reaches tool code without
  a user_id MUST fail closed.
- Tool functions MUST NOT take a user_id parameter, in any shape.
  This is enforced by code review, not by the runtime.
- Onboarding tickets MUST be bound to the requesting browser
  (user-agent + source IP) so a leaked URL can't be replayed from
  another device.
- A leaked or stolen token from one user MUST NOT yield access to
  another user's Garmin data. Tested under scenario S1 in
  [chapter 10](10-quality-requirements.md#s1--per-user-data-isolation-security).

## Web security headers

Browser-reachable endpoints (`/onboard`, `/onboard/status`, the
OAuth flow's HTML pages) render forms that take user credentials.
The TLS terminator (Caddy) MUST add a fixed set of response headers
on every response routed to a browser:

| Header | Value | Why |
|---|---|---|
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` | Block TLS downgrade |
| `X-Frame-Options` | `DENY` | Block clickjacking the credential form |
| `X-Content-Type-Options` | `nosniff` | Block MIME confusion |
| `Referrer-Policy` | `no-referrer` | Don't leak `?ticket=…` to third parties |
| `Content-Security-Policy` | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'` | Confine fetch sources to same-origin |
| `Permissions-Policy` | `geolocation=(), microphone=(), camera=()` | Drop unused powerful features |

### Requirements

- All six headers above MUST be set on every browser-facing
  response.
- `Server` MUST be removed from responses (don't advertise the
  reverse proxy version).
- The CSP MUST forbid `frame-ancestors`; if a page needs framing for
  a future feature, the CSP MUST be updated explicitly with a named
  allowlist.

## CSRF protection

State-changing endpoints reachable from a browser session
(`POST /onboard/credentials`, `POST /onboard/mfa`) MUST carry a
per-session CSRF token. The token is generated at session creation
(`OnboardingManager.create_session`), stored in the
`OnboardingSession`, embedded as a hidden form field, and
delivered as a `__Host-`-prefixed cookie scoped to the ticket.
Submission MUST validate that the form value matches the cookie.

### Requirements

- Every state-changing browser endpoint MUST accept a per-session
  CSRF token and reject the request if it's missing or doesn't
  match.
- The CSRF cookie MUST be `__Host-`-prefixed, `Secure`, `HttpOnly`,
  and `SameSite=Strict`.
- The CSRF token MUST NOT appear in URLs (it's a form field or
  header only).

## Trust boundary

The single trust boundary inside the deployment is the
`mcp_internal` Docker bridge network: Caddy is on it,
`garmin-mcp` is on it, nothing else is. Public traffic enters
through Caddy; the application port (`:8000`) is `expose:`d but
never `ports:`d.

This shape determines what we trust and what we don't:

| Source | Trust | Mechanism |
|---|---|---|
| Public TLS terminator (Caddy) | Trusted | Same-host, container we built |
| Inbound HTTP to Caddy | Untrusted | Sanitized at the proxy layer |
| `X-Forwarded-For` from Caddy | Trusted | Caddy strips inbound and writes the real client IP |
| `X-Forwarded-For` from any other source | Untrusted | uvicorn `forwarded_allow_ips` MUST be scoped to the bridge subnet |

### Requirements

- uvicorn `forwarded_allow_ips` MUST be set to the Docker bridge
  subnet only (or `127.0.0.1` in single-host dev mode), never `*`.
- Caddy MUST strip any inbound `X-Forwarded-For` and re-build it
  from `{remote_host}` so the application sees the actual TCP-level
  source IP, not whatever the client put in the header.
- The application port MUST NOT be published with `ports:` in
  production compose; only Caddy publishes ports.

## Logging

Two streams:

- **Application log** (Python `logging`, default `INFO`) — goes to
  stdout, captured by Docker. Standard ops + diagnostics.
- **Audit log** (`auth/audit.py`) — one JSON line per OAuth event
  (`register.success`, `authorize.start`, `token.issued`, …) in
  `/var/log/garmin-mcp/audit-YYYY-MM-DD.log`. Failures to write are
  swallowed — auditing must never break a request.

The audit log is **hash-chained**: each line carries a `prev_hash`
field equal to the SHA-256 of the previous line. A separate CLI
(`garmin-mcp-verify-audit`) walks a file and reports any broken
link.

### Requirements

- The audit log MUST be hash-chained with SHA-256 across consecutive
  entries within the same daily file.
- Neither log MUST contain user passwords, MFA codes, JWT bearer
  tokens, refresh tokens, OAuth client secrets, or Garmin OAuth
  blobs (encrypted or otherwise). Code review enforces this; the
  audit log fields are an explicit allowlist.
- A failed log write MUST NOT raise into the request handler.
- Log retention is the operator's call (current default: 90 days
  via the host's logrotate / docker log driver, off-site backup
  via `deploy/backup-offsite.sh`).

## Rate limiting

Defense in depth around the publicly-reachable endpoints:

- **Network layer**: Caddy `rate_limit` directive on `/register`
  (default 10 req/min per IP). Stops bot scanners before they hit
  the application.
- **Per-IP token bucket on `/register`** — `RegistrationGuard` uses a
  `TokenBucket` (5 successful registrations / IP / hour). Persisted in
  SQLite so an attacker restart-bot can't reset the counter.
- **Global cap on `oauth_clients`** — refuse new registrations when the
  table exceeds 10,000 rows.
- **TTL on unused / idle clients** — background cleanup loop drops rows
  that registered but never exchanged a token (>24 h) or haven't been
  used in 90 days.
- **Per-user tool-call bucket** — `ToolCallGuard` consumes tokens from
  a per-user bucket on every Garmin API call, plus a global outbound
  bucket so one user can't burn the whole VPS's Garmin budget.
- **Optional shared lockdown token** — when `MCP_REGISTRATION_TOKEN` is
  set, `/register` requires it as a Bearer header; useful while the
  deploy is still being tested.

### Requirements

- `/register` MUST be guarded by a per-IP token bucket at the
  application layer; the network-layer Caddy limit is a defense
  layer, not a substitute.
- Tool calls MUST consume tokens from both a per-user bucket and a
  global outbound bucket; exhaustion of either MUST yield a clear
  rate-limit error and prevent the upstream Garmin call.
- A failed registration (any cause) MUST consume a per-IP token so
  trial-and-error attacks pay the same cost as success.
- Bucket state MUST survive process restart (persisted in SQLite).

## Error handling and input validation

Three categories with distinct shapes:

| Class | Example | Surface |
|---|---|---|
| **Expected user errors** | bad MFA, invalid date format | Translated to friendly text in the response (HTML for /onboard, JSON-RPC error for tools) |
| **Auth failures** | bad signature, expired token | 401 + WWW-Authenticate; client should refresh |
| **Internal errors** | DB locked, Entra unreachable | 5xx + audit-logged; user sees a generic message |

The provider raises `mcp.server.auth.provider.TokenError` /
`RegistrationError` for protocol-level errors, which FastMCP converts
to the right RFC 6749 JSON shape automatically.

### Requirements

- Error responses MUST NOT enumerate users: a request with an
  unknown email MUST return the same shape (and within timing
  tolerance the same latency) as a wrong-password attempt.
- Upstream provider error text (Entra, Garmin) MUST NOT be echoed
  verbatim to the user; it's logged server-side and a generic
  message is returned.
- Every tool input that becomes part of a Garmin URL MUST be
  validated against a regex or length cap. Free-form strings (e.g.
  `search` parameters) MUST be capped at a sensible length (≤ 200
  characters) and URL-encoded.
- Date parameters MUST match `^\d{4}-\d{2}-\d{2}$` (already
  enforced in the nutrition + activity tools).
- UUID parameters MUST match the canonical UUID regex (already
  enforced where used).

## Concurrency model

- One uvicorn worker, single asyncio event loop, no multiprocessing.
- `asyncio.to_thread()` wraps every blocking storage call so the loop
  stays responsive under load.
- The onboarding worker runs in a daemon `threading.Thread` because
  `garth.login()` is purely synchronous; it communicates back to the
  async layer via a `threading.Event` + `queue.Queue` pair.
- The MCP session manager uses anyio task groups internally; we just
  use its lifespan context.

### Requirements

- The per-user Garmin client cache (`MultiUserClientCache`) MUST be
  race-free under concurrent miss: two simultaneous
  `get_or_load(user_id)` calls MUST result in exactly one decrypt +
  build, with the second caller waiting on the first.
- The `users` table insert (`get_or_create_user`) MUST be
  idempotent under concurrent first-sign-ins for the same
  `(entra_sub, entra_tid)`. Use `INSERT ... ON CONFLICT DO NOTHING`
  + re-select rather than catching the integrity exception.
- Onboarding sessions MUST be bounded in count (hard cap on
  concurrent active sessions; current default 10) and lifetime (≤ 5
  min ticket TTL; aggressive eviction of terminal sessions in the
  background cleanup task).
- Onboarding worker threads MUST exit within `mfa_timeout_seconds`
  of the last user interaction (current target: 90 s after MFA
  prompt with no response).
- MFA submission MUST be atomic: the attempt counter increment and
  the state transition MUST happen under the session lock so
  concurrent submissions can't bypass `MAX_MFA_ATTEMPTS`.

## Schema evolution

Rules:

1. New tables only; never drop or alter existing columns.
2. Bump `SCHEMA_VERSION` in `auth/storage.py` for tracking, but the DDL
   is `CREATE TABLE IF NOT EXISTS` so the same DDL runs on fresh and
   upgraded DBs.
3. The init code accepts a *lower* version (forward upgrade), refuses a
   *higher* version (the binary is too old for the DB).

### Requirements

- The deployment MUST be single-writer. Two server processes against
  the same SQLite file is unsupported; the schema-init logic isn't
  serialized across processes. Multi-instance HA requires migrating
  to Postgres (out of scope; see chapter 11 R1).
- Schema changes MUST be additive only (new tables, new columns
  with defaults). Drops or alters require a real migration framework
  not yet present.

## Configuration

All runtime config flows through env vars (see `deploy/env.example`).
No config files; nothing to YAML-parse on startup; one source of truth
that maps cleanly to `docker-compose.yml`'s `env_file:`.

Required vars are read with `os.environ[...]` (KeyError = fail fast at
startup); optional vars use `os.environ.get(...)` with defaults.

### Requirements

- Secrets MUST come from environment variables backed by a
  mode-0600 file owned by root on the host (`/etc/garmin-mcp/env`)
  or, equivalently, from Docker secrets mounted under
  `/run/secrets/`.
- No required secret MUST have a default value baked into the
  source (a missing `GARMIN_MCP_DATA_KEY` MUST refuse to start, not
  silently use a placeholder).
- Secrets MUST NOT be logged. The application log MUST NOT echo
  the contents of `os.environ` at startup.

## Container hardening

The Dockerfile + compose stack runs each container with the minimum
privileges it needs:

- Non-root user (uid/gid 1000) inside both `garmin-mcp` and `caddy`.
- Read-only root filesystem; writable mounts only for the SQLite
  data volume, audit log volume, and a tmpfs at `/tmp`.
- All Linux capabilities dropped except `NET_BIND_SERVICE` on Caddy
  (so it can bind to :80/:443).
- `no-new-privileges:true` on both services.
- Hard memory + CPU + PID limits to prevent host-wide resource
  exhaustion.
- Caddy's admin API (`localhost:2019`) is disabled in the
  Caddyfile (`{ admin off }`) so a shell-in-container compromise
  can't reload the proxy config.

### Requirements

- Both services MUST run as non-root.
- Both services MUST drop ALL Linux capabilities; required ones
  added back explicitly with `cap_add`.
- Both services MUST set `read_only: true` on the root filesystem;
  writable paths are explicit volumes or tmpfs.
- Both services MUST set `security_opt: [no-new-privileges:true]`.
- Both services MUST have memory and CPU limits set; the host MUST
  NOT be at risk from a single misbehaving container.
- The reverse proxy's admin API MUST be disabled.

## Supply chain integrity

The deploy artifacts (Dockerfiles, compose, `uv.lock`) define the
exact bytes of every dependency:

- All `FROM` lines in `deploy/Dockerfile` and `deploy/Dockerfile.caddy`
  MUST be pinned to an immutable SHA-256 digest, not just a tag.
- `uv.lock` MUST be checked in and verified against `pyproject.toml`
  in CI (`uv lock --check`).
- `pip-audit` runs against the resolved dependency set on every PR
  and weekly on a schedule. A new CVE blocks merges until either
  fixed, or accepted with a documented exception in
  `doc/11-risks-and-technical-debt.md`.
- The MS Graph Bicep extension and any other third-party
  manifest-bound tooling MUST be pinned to a specific version
  string, not `latest`.

### Requirements

- Base image digests MUST be refreshed at least monthly to pick up
  upstream security patches; refresh procedure documented in
  `deploy/README.md`.
- A new CVE in any dependency MUST either land a fix in the same
  PR or an explicit exception in chapter 11 with rationale and an
  expiry date.

## Operational health

The deployment includes liveness affordances aimed at the operator
running it on a single VPS:

- `GET /healthz` MUST verify that the application can read from
  SQLite (`COUNT(*) FROM oauth_clients` or similar). A "healthy"
  response with a broken DB underneath is worse than a 503.
- The Docker HEALTHCHECK uses the same `/healthz` endpoint; an
  unhealthy container is restarted by Docker according to the
  compose `restart: unless-stopped` policy.
- Garmin upstream session expiry (the ~6-month garth token TTL)
  MUST be detected automatically: a 401 from Garmin during a tool
  call MUST invalidate the user's cached client and surface a
  structured error pointing the user at `/onboard` for
  re-authentication.
- The audit-anomaly background task MUST log a WARNING when the
  registration rate exceeds threshold (currently 10 successful
  registrations / 5-min window), so the operator can see attacks
  forming without parsing the raw audit log.
- The off-site backup script (`deploy/backup-offsite.sh`) MUST be
  schedulable from cron and MUST not require server downtime; the
  SQLite online-backup API gives a consistent snapshot.

### Requirements

- `/healthz` MUST report unhealthy (HTTP 503) when SQLite is
  unreachable or the audit-log writer is failing.
- The container MUST shut down cleanly on SIGTERM: in-flight HTTP
  requests are drained with a bounded grace period; pending
  onboarding sessions are abandoned with a clear log line; the
  cleanup loop is cancelled.
- Operations that require restart (key rotation, schema migration)
  MUST be documented in `deploy/README.md` with a downtime
  estimate.
