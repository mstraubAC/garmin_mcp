# Hardening — round 2

The first hardening pass (PRs #10–#26) closed every item in the
original [`doc/11-risks-and-technical-debt.md`](doc/11-risks-and-technical-debt.md)
plus the items the rollout plan explicitly deferred to step 9.

This document collects findings from a **fresh audit pass** run after
round 1 landed: parallel reviews of the web/HTTP surface, the
container/deployment stack, and the application code. The bar for
inclusion here is "found something the round-1 plan didn't address" —
not a re-litigation of items already shipped.

Same shape as the previous plan: each item carries Why / Where / What
/ Files / Tests / Size, plus an explicit **Verified** note when the
finding came from an automated review and the conclusion needed
checking by hand.

When all tracks land, **`HARDENING.md` should be deleted again** and
the relevant doc/ chapters updated to reflect the closed risks.

---

## Pre-flight finding

`pip-audit` against the current `uv.lock` reports **one** CVE
(down from 8 before round 1):

| Package | Version | Fix | Notes |
|---|---|---|---|
| pygments | 2.19.2 | 2.20.0 | Transitive via `pytest`. Bumping `pytest>=9.0.4` (or the next available) should pull a fixed pygments. |

Bundle this with PR A1 below so CI doesn't go red.

---

## Track A — critical surface (HIGH)

### A1. Set security response headers in Caddy

**Why.** Onboarding renders a real HTML form (email, password, MFA
code) with zero clickjacking or MIME-sniffing protections. Without
HSTS, a downgrade attack that strips TLS once is enough; without
`X-Frame-Options` / CSP, the form is iframable.

**Where.** [`deploy/Caddyfile`](deploy/Caddyfile) (no header
directives); [`src/garmin_mcp/auth/onboarding_routes.py`](src/garmin_mcp/auth/onboarding_routes.py)
(`_layout()` returns bare HTML without any meta-equivalents).

**What.** Add to the Caddyfile site block:

```caddy
header {
    Strict-Transport-Security "max-age=31536000; includeSubDomains"
    X-Frame-Options            "DENY"
    X-Content-Type-Options     "nosniff"
    Referrer-Policy            "no-referrer"
    Content-Security-Policy    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    Permissions-Policy         "geolocation=(), microphone=(), camera=()"
    -Server
}
```

The CSP needs `'unsafe-inline'` for the styles in `_layout()` only —
external JS is already self-served (vendored htmx, PR #20). Move
the inline `<style>` block to a static file in a follow-up if we
want to drop the `'unsafe-inline'`.

**Files.** `deploy/Caddyfile`,
[`doc/08-crosscutting-concepts.md`](doc/08-crosscutting-concepts.md)
(add a "Web headers" subsection).

**Tests.** Bring the stack up locally; `curl -Ik
https://localhost/onboard` shows all six headers. Add a small
integration assertion in `tests/integration/test_onboarding_flow.py`
that the responses include the headers we set in the **app**
(harder for Caddy-level headers — those need a live caddy in front).

**Size.** Small — ~15 lines in Caddyfile + ~5 lines of doc.

---

### A2. Stop trusting `X-Forwarded-For` from anywhere

**Why.** `forwarded_allow_ips="*"` in [`server.py:309`](src/garmin_mcp/server.py)
tells uvicorn to accept `X-Forwarded-For` from any sender. With
Caddy in front this is fine because nothing else can reach :8000 —
**but** the `RegistrationGuard.check_per_ip()` rate limiter (when we
wire it in PR A3) reads the client IP from that header. If the
guard ever runs without Caddy correctly stripping the inbound header
(misconfig, direct port exposure during debugging, future migration
to a different ingress), an attacker spoofs `X-Forwarded-For` and
bypasses per-IP limits trivially.

Defense-in-depth: lock down what uvicorn trusts to the Docker bridge
that Caddy is on.

**Where.** [`src/garmin_mcp/server.py:309`](src/garmin_mcp/server.py)
(`forwarded_allow_ips="*"`); [`deploy/Caddyfile`](deploy/Caddyfile)
(propagates the header).

**What.** Two changes:

1. Replace `forwarded_allow_ips="*"` with the Docker bridge subnet:
   `forwarded_allow_ips=os.environ.get("UVICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")`,
   default `127.0.0.1`. In compose, set it explicitly to the
   `mcp_internal` bridge subnet (e.g. `172.18.0.0/16` — read from
   `docker network inspect`, or set a fixed subnet in the compose
   file's network spec).
2. In Caddyfile, stop forwarding `X-Forwarded-For` verbatim — let
   Caddy build the header from `{remote_host}` itself, which is
   what `request_header` already does. **But** also explicitly
   strip any inbound `X-Forwarded-For` from clients before the
   reverse_proxy:
   ```caddy
   request_header -X-Forwarded-For
   reverse_proxy garmin-mcp:8000 {
       header_up X-Forwarded-For {remote_host}
       ...
   }
   ```

**Files.** `deploy/docker-compose.yml` (fix bridge subnet), `deploy/Caddyfile`,
`src/garmin_mcp/server.py`,
`doc/08-crosscutting-concepts.md` (note the trust boundary).

**Tests.** Wire the per-IP guard from A3 with a unit test that
sends `X-Forwarded-For: 1.2.3.4` directly to uvicorn (bypassing
Caddy in the test) and confirms the IP it actually used was the
ASGI `client.host`, not the spoofed header.

**Size.** Medium — ~30 lines.

---

### A3. Wire `RegistrationGuard.check_per_ip()` into `/register`

**Why.** The guard is built and configured in
[`server.py:285-291`](src/garmin_mcp/server.py) and tested in
[`tests/unit/test_auth_throttle.py::test_check_per_ip_uses_bucket`](tests/unit/test_auth_throttle.py),
but **`provider.register_client()` never calls
`self.guard.check_per_ip()`**. Only `check_field_lengths`,
`check_redirect_uri`, and `under_global_cap` actually run on the
hot path.

That means the headline defense from `doc/08-crosscutting-concepts.md`
("per-IP token bucket on `/register`: 5 successful registrations / IP /
hour") is documentation, not behavior. An unlimited number of DCR
registrations from one IP is currently allowed up to the 10 000-row
global cap.

**Verified.** Confirmed by hand: `grep -rn "check_per_ip" src/`
returns the definition and the test, no production caller.

**Where.**
[`src/garmin_mcp/auth/provider.py:97-141`](src/garmin_mcp/auth/provider.py)
(`register_client`).

**What.**
1. Get the client IP into `register_client`. The MCP SDK doesn't
   currently pass the request to the provider — we need a
   ContextVar similar to `current_user_id` (e.g.
   `current_register_ip`) set by a small Starlette middleware on
   `POST /register`.
2. Inside `register_client`, after the field-length and redirect-URI
   checks, call `await self.guard.check_per_ip(ip)`. Raise
   `RegistrationError(error="server_error", error_description="rate
   limit exceeded; try again later")` on miss.
3. Persist `register_ip` in the storage row (already columned, never
   populated — see same file, line 134).

**Files.** `src/garmin_mcp/auth/provider.py`, `src/garmin_mcp/server.py`
(middleware), `tests/integration/test_oauth_flow.py` (add a "11th
registration in an hour returns server_error" test),
`doc/08-crosscutting-concepts.md` (no change — current text already
matches what we're now actually doing).

**Tests.** Integration test that hammers `/register` from a loop and
verifies the 6th call within an hour returns the error. Existing
`test_check_per_ip_uses_bucket` covers the bucket itself.

**Size.** Medium — ~80 lines.

---

### A4. Pin Docker base images by digest

**Why.** Tags are mutable. A future
`python:3.13-slim` rebuild upstream — or a compromised registry
push — silently changes our runtime contents on the next
`docker compose build`. There's no way to detect this without
comparing SHAs.

**Where.**
- [`deploy/Dockerfile:9`](deploy/Dockerfile) — `ghcr.io/astral-sh/uv:0.5-python3.13-bookworm-slim`
- [`deploy/Dockerfile:30`](deploy/Dockerfile) — `python:3.13-slim`
- [`deploy/Dockerfile.caddy`](deploy/Dockerfile.caddy) — `caddy:2-builder`, `caddy:2-alpine`
- Legacy [`Dockerfile`](Dockerfile) at repo root — `python:3.12-slim` (still in tree from upstream; should be removed entirely or also pinned)

**What.**
1. For each `FROM`, run `docker buildx imagetools inspect <tag>` and
   replace the tag with `<tag>@sha256:<digest>`.
2. Document in `deploy/README.md` how to refresh digests
   periodically (e.g. monthly) — a small helper script
   `deploy/scripts/refresh-digests.sh` could automate this.
3. Remove the legacy root `Dockerfile` and `docker-compose.yml`
   (upstream stdio leftovers; `deploy/` is the canonical path).

**Files.** `deploy/Dockerfile`, `deploy/Dockerfile.caddy`, repo-root
`Dockerfile` and `docker-compose.yml` (delete), `deploy/README.md`,
new `deploy/scripts/refresh-digests.sh`.

**Tests.** `docker buildx build` succeeds in CI (already covered).
Add a CI step that warns if a `FROM` line lacks `@sha256:`
(`grep -E '^FROM[^@]+$' deploy/Dockerfile* && exit 1`).

**Size.** Small — ~20 lines + a helper script.

---

## Track B — container hardening (MEDIUM)

### B1. Drop capabilities and set `read_only` rootfs in compose

**Why.** Both containers run with the default Linux capability set.
A Python or Caddy RCE has more leverage than necessary
(CAP_NET_RAW, CAP_DAC_OVERRIDE, …). Cheap defense in depth.

**Where.** [`deploy/docker-compose.yml`](deploy/docker-compose.yml)
(no `cap_drop`, no `security_opt`, no `read_only`).

**What.** Add to both services:

```yaml
security_opt: [no-new-privileges:true]
cap_drop: [ALL]
read_only: true
tmpfs:
  - /tmp:size=64M           # uvicorn / sqlite tempfiles
```

For Caddy add `cap_add: [NET_BIND_SERVICE]` so it can bind :80/:443.
For garmin-mcp no caps need to be added (uvicorn binds 8000 which
is unprivileged).

The `read_only` rootfs requires SQLite WAL files to live on a
writable mount. They already do (`/var/lib/garmin-mcp` named
volume), so this should Just Work — but verify on first run.

**Files.** `deploy/docker-compose.yml`, `deploy/README.md`
(troubleshooting note for read-only-related errors).

**Tests.** Bring stack up; verify both healthchecks green and
onboarding still works end-to-end (write to SQLite is the
canary).

**Size.** Small — ~15 lines.

---

### B2. Add resource limits in compose

**Why.** A runaway tool call (Garmin API loop, malformed JSON
parsing) or a memory leak can OOM the entire VPS — the kernel will
pick a victim, and on a small CX11/CAX11 it's often `dockerd`
itself. Hard limits make the failure mode "one container dies and
restarts" instead of "host reboots".

**Where.** `deploy/docker-compose.yml` (no `mem_limit`, `cpus`,
`pids_limit`).

**What.** Add to garmin-mcp:

```yaml
mem_limit: 512m
cpus: 1.0
pids_limit: 256
```

And to caddy (smaller — it does very little CPU work):

```yaml
mem_limit: 128m
cpus: 0.5
pids_limit: 64
```

Numbers are reasonable starting points for a personal-scale VPS;
revise upward if real traffic exceeds them.

**Files.** `deploy/docker-compose.yml`, `doc/07-deployment-view.md`
(note the limits in the "Nodes" subsection).

**Tests.** Stack starts; observe `docker stats` to confirm limits
apply. Stress-test with `ab -n 1000 -c 50 …/healthz` to verify
the container doesn't get OOM-killed under burst.

**Size.** Small — ~10 lines.

---

### B3. Make `/healthz` actually verify the database

**Why.** Current `/healthz` returns `{"status":"ok"}` regardless of
SQLite state. A corrupted DB, locked file, or permission failure
keeps the container in "healthy" — Docker won't restart it, Caddy
keeps proxying, requests fail with 500s.

**Where.** [`src/garmin_mcp/server.py:131-132`](src/garmin_mcp/server.py)
(`healthz` returns a static dict).

**What.** Run a trivial read against `Storage`:

```python
async def healthz(_: Request) -> Response:
    try:
        # Cheap read; fails if the DB is locked / unreadable.
        await asyncio.to_thread(_storage.count_clients)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        logging.getLogger(__name__).warning("healthz failed: %s", e)
        return JSONResponse({"status": "fail"}, status_code=503)
```

The handler needs access to the Storage instance — same pattern as
the auth provider: inject via closure in `make_app`, capture into
the route function.

**Files.** `src/garmin_mcp/server.py`,
`tests/integration/test_http_server.py` (new test for the failing
case — pass a mock Storage that raises).

**Tests.** Existing `/healthz` test still passes. New test confirms
503 when storage raises.

**Size.** Small — ~25 lines including test.

---

### B4. Disable Caddy's admin API

**Why.** Caddy's default admin API listens on `localhost:2019`.
It's not exposed externally (compose doesn't publish it), but
**any process inside the caddy container** can hit it: dump the
live config (which includes the upstream URL), reload routes,
shut Caddy down. RCE in the caddy container becomes lateral
movement at no extra cost.

**Where.** [`deploy/Caddyfile`](deploy/Caddyfile) — no
`{ admin off }` global option.

**What.** Add a global option block at the top of the Caddyfile:

```caddy
{
    admin off
    email admin@example.com    # already there
}
```

Day-2 ops on Caddy are rare for this deployment (no dynamic config
changes); if the API is needed we can enable it bound to
`localhost:2019` only and document the
`docker compose exec caddy curl localhost:2019/...` flow.

**Files.** `deploy/Caddyfile`, `doc/07-deployment-view.md` (one-line
note).

**Tests.** `docker compose exec caddy curl -fs localhost:2019/`
returns "connection refused" after the change.

**Size.** Small — 1 line + doc.

---

## Track C — application correctness (MEDIUM)

### C1. Cap onboarding session memory and worker-thread lifetime

**Why.** Two related leaks in
[`src/garmin_mcp/auth/onboarding.py`](src/garmin_mcp/auth/onboarding.py):

- **Sessions in COMPLETE / FAILED / EXPIRED state are never
  evicted.** `_evict_expired_locked()` (line 142) only deletes
  rows whose `expires_at` is past plus a 60-second grace, and only
  runs on `create_session()` — i.e. evictions are coupled to new
  traffic, not time. A burst of 1000 successful onboardings leaves
  1000 entries in `self._sessions` until someone starts another
  onboarding.
- **MFA timeout is 5 minutes.** Each session that hits
  AWAITING_MFA but never receives a code holds a daemon thread
  blocked on `Queue.get(timeout=300)`. Sustained probing creates
  many concurrent threads.

**Verified.** Confirmed by reading the eviction logic line by line
and the worker thread loop in `_run_login()`.

**Where.**
- [`onboarding.py:143-149`](src/garmin_mcp/auth/onboarding.py)
  (`_evict_expired_locked` — only-on-create eviction)
- [`onboarding.py:40`](src/garmin_mcp/auth/onboarding.py)
  (`DEFAULT_MFA_TIMEOUT_SECONDS = 5 * 60`)
- [`onboarding.py:172-178`](src/garmin_mcp/auth/onboarding.py)
  (worker thread)

**What.**
1. Wire eviction into the existing `cleanup_loop` background task
   (currently it only handles SQLite). Run it every minute; in
   addition to today's checks, walk
   `OnboardingManager._sessions` and drop terminal entries whose
   state changed > 60 s ago, plus expired-but-not-yet-marked
   sessions.
2. Cut `DEFAULT_MFA_TIMEOUT_SECONDS` to 90 seconds. Real users
   take 30 s to fish the code out of their email; 90 s is plenty.
3. Add a hard cap on concurrent worker threads
   (`max_concurrent_sessions` already exists at line 84; raise it
   to a noisy enforcement instead of a silent first-rejected
   pattern — log a WARNING when we cap).

**Files.** `src/garmin_mcp/auth/onboarding.py`,
`src/garmin_mcp/maintenance/cleanup.py`,
`tests/unit/test_auth_onboarding.py` (timeout + eviction tests),
`doc/06-runtime-view.md` (note the cleanup contract).

**Tests.** Existing onboarding tests pass with the shorter MFA
timeout. New test: create 5 sessions, simulate them reaching
COMPLETE, run one cleanup tick, assert all 5 are evicted.

**Size.** Small — ~50 lines.

---

### C2. Fix the cache double-load race

**Why.** [`MultiUserClientCache.get_or_load`](src/garmin_mcp/user_context.py)
releases its lock between the cache miss check and the actual load
+ insert. Two requests for the same `user_id` arriving
simultaneously can both miss, both decrypt the token blob, both
build a fresh `Garmin` instance — only one ends up in the cache,
the other is discarded. That wastes 5–10 ms of `garth.loads()` and,
more importantly, makes the cache hit-rate metrics misleading.

Also, [`storage.get_or_create_user`](src/garmin_mcp/auth/storage.py)
does check-then-insert without `ON CONFLICT`. Two concurrent first
sign-ins for the same `(entra_sub, entra_tid)` will race on the
INSERT; one will hit the UNIQUE constraint and raise. That's
correct behavior (the second request retries naturally), but it
spams an exception in logs.

**Verified.** Checked the lock scope by reading
`user_context.py:120-148` and the SQL in `storage.py:417-442`.

**Where.**
- [`src/garmin_mcp/user_context.py:120-148`](src/garmin_mcp/user_context.py)
- [`src/garmin_mcp/auth/storage.py:417-442`](src/garmin_mcp/auth/storage.py)

**What.**
1. **Cache:** keep the lock held across the full miss-load-insert
   path (or use a per-user lock with a `defaultdict(asyncio.Lock)`
   pattern if the broad lock causes contention — likely overkill
   at our scale).
2. **User table:** `INSERT ... ON CONFLICT (entra_sub, entra_tid) DO NOTHING
   RETURNING *` and re-SELECT if the insert was a no-op. SQLite
   3.35+ supports this; we're on whatever Debian ships, but
   modern Pythons bundle 3.40+.

**Files.** `src/garmin_mcp/user_context.py`,
`src/garmin_mcp/auth/storage.py`,
`tests/unit/test_user_context.py` (concurrent miss test using
`asyncio.gather`),
`tests/unit/test_auth_storage.py`.

**Tests.** Concurrent test: 10 parallel `get_or_load("u1")` calls;
assert `garmin_factory` was called exactly once.

**Size.** Small — ~40 lines.

---

### C3. JWT signing key rotation (kid header)

**Why.** [`JwtSigner`](src/garmin_mcp/auth/jwt.py) reads
`JWT_SIGNING_KEY` once at startup. Rotating the key revokes
**every in-flight access token instantly** — every connected
Claude session has to refresh, which works but isn't graceful.
We have a Fernet rotation tool (PR #21); access-token rotation
got skipped in round 1.

**Where.** [`src/garmin_mcp/auth/jwt.py:44-105`](src/garmin_mcp/auth/jwt.py)
— single key, no `kid` header.

**What.**
1. `JwtSigner.__init__` accepts a list of `(kid, key)` pairs. The
   first entry is the signer; the rest are accepted for verify
   only.
2. `issue()` writes `kid` into the JWT header.
3. `verify()` looks up the key by `kid`; falls back to the legacy
   single-key behavior if the token has no `kid` (back-compat
   during the rollout).
4. Env-var format: keep `JWT_SIGNING_KEY` for the active key,
   add `JWT_SIGNING_KEYS_PREVIOUS` (comma-separated `kid:b64key`
   tuples) for the verify-only set. Operator workflow: bump
   active key, move the old one into PREVIOUS, wait the access
   token TTL (1 h), then drop it.
5. Document in `deploy/README.md`'s "Day-2 ops" section.

**Files.** `src/garmin_mcp/auth/jwt.py`, `src/garmin_mcp/server.py`
(env wiring), `tests/unit/test_auth_jwt.py` (multi-key tests),
`doc/09-architecture-decisions.md` (extend ADR-006 or new ADR-007),
`deploy/env.example`, `deploy/README.md`.

**Tests.** Token signed with the previous key still verifies until
removed from PREVIOUS. Token signed with no `kid` (pre-rollout)
still verifies during the back-compat window.

**Size.** Medium — ~120 lines.

---

### C4. CSRF protection on `/onboard` POSTs

**Why.** `POST /onboard/credentials` and `POST /onboard/mfa`
accept the ticket as a form field with no per-request token. A
malicious page that knows or guesses a ticket can submit on the
user's behalf. The window is small (5-min ticket TTL, single-use
flow) but the consequence is the user's Garmin password is
delivered to the attacker.

The bar to pull this off is "knows the ticket" — and tickets do
appear in browser history, log files, and Referer headers, so
"knows the ticket" isn't crazy.

**Where.**
- [`src/garmin_mcp/auth/onboarding_routes.py:163-180`](src/garmin_mcp/auth/onboarding_routes.py)
  (`submit_credentials`)
- [`src/garmin_mcp/auth/onboarding_routes.py:202-215`](src/garmin_mcp/auth/onboarding_routes.py)
  (`submit_mfa`)

**What.** Generate a CSRF token at session creation, store it in
the `OnboardingSession` and as a `__Host-` cookie scoped to the
ticket. Embed it as a hidden form field in the rendered HTML;
verify on POST.

Alternative: a same-origin check via `Sec-Fetch-Site: same-origin`
header. Cheaper but only a partial mitigation (relies on browser
behavior, not on our validation).

**Files.** `src/garmin_mcp/auth/onboarding.py`,
`src/garmin_mcp/auth/onboarding_routes.py`,
`tests/integration/test_onboarding_flow.py`.

**Tests.** POST with no CSRF token returns 400. POST with
ticket-A's token but ticket-B's payload returns 400.

**Size.** Medium — ~80 lines.

---

### C5. Bind onboarding tickets to user-agent + IP

**Why.** A ticket leaked via Referer / browser history / log file
is useful for ~5 minutes. Binding it to the requesting browser
shrinks that window to "the same browser, before the user
navigates away". Cheap.

**Where.** [`src/garmin_mcp/auth/onboarding.py:114-124`](src/garmin_mcp/auth/onboarding.py).

**What.**
1. `create_session()` records the requesting IP and a hash of the
   `User-Agent` header.
2. `submit_credentials` and `submit_mfa` re-check both — mismatch
   → reject with a clear error.

**Files.** `src/garmin_mcp/auth/onboarding.py`,
`src/garmin_mcp/auth/onboarding_routes.py` (pass IP + UA in),
`tests/integration/test_onboarding_flow.py`.

**Tests.** Same-IP/UA: existing tests still pass. Different IP:
new test confirms rejection.

**Size.** Small — ~40 lines.

---

### C6. Tighten DCR `grant_types` / `response_types` allowlist

**Why.** [`provider.register_client`](src/garmin_mcp/auth/provider.py:97)
accepts whatever shape the client sends in those fields. We only
support `authorization_code` (with PKCE) + `refresh_token`. A
client that registers with `grant_types=["password"]` or
`response_types=["token"]` would be persisted in the DCR registry
and could attempt those flows; the SDK's `/token` endpoint should
reject them, but **the registration itself shouldn't have been
accepted in the first place**. Defense in depth.

**Verified.** Confirmed `register_client` only checks length +
redirect URI; no enum validation.

**Where.**
[`src/garmin_mcp/auth/provider.py:97-141`](src/garmin_mcp/auth/provider.py).

**What.** Whitelist:
```python
ALLOWED_GRANT_TYPES = {"authorization_code", "refresh_token"}
ALLOWED_RESPONSE_TYPES = {"code"}
ALLOWED_AUTH_METHODS = {"client_secret_basic", "client_secret_post", "none"}
```

Reject the registration with `RegistrationError(error="invalid_client_metadata", …)`
on any mismatch.

**Files.** `src/garmin_mcp/auth/provider.py`,
`tests/integration/test_oauth_flow.py`.

**Size.** Small — ~30 lines.

---

## Track D — minor (LOW)

### D1. Sanitize Entra error messages in `/callback`

**Why.** [`provider.py:202`](src/garmin_mcp/auth/provider.py)
returns `f"entra exchange failed: {e}"` to the user. If Entra ever
returns a distinguishing error for "user not in tenant" vs.
"invalid code", that's a small user-enumeration leak. Also makes
the user-facing error noisier than it needs to be.

**What.** Log the original `EntraError` server-side at WARNING;
return a generic "Sign-in failed; please try again." to the user.

**Size.** Trivial.

### D2. Validate freeform `search` length on tool inputs

**Why.** [`nutrition.py:get_custom_foods`](src/garmin_mcp/nutrition.py)
takes a `search` string of arbitrary length. URL-encoding (already
in place) prevents injection but not "100KB search string causes
Garmin API to reject". Cap at e.g. 200 chars and reject with a
clear message early.

**Size.** Trivial.

### D3. Schema upgrade serialization across simultaneous starts

**Why.** [`storage._init_schema`](src/garmin_mcp/auth/storage.py)
isn't safe against two server processes starting against the same
SQLite file at once. Today we deploy one container so this can't
happen. If a future deploy uses two replicas behind shared
storage, this needs to be revisited.

**What.** Document the constraint as an ADR addendum
(`doc/09-architecture-decisions.md`, ADR-003 or new): "single-writer
process; multi-instance requires Postgres". Don't fix until needed.

**Size.** Doc-only.

---

## Verified-not-issues (false positives from the audit)

Listed for transparency so a future review doesn't re-litigate
them.

| Audit claim | Verdict | Why |
|---|---|---|
| Open redirect in `_issue_code_for` (claude_redirect_uri reused without re-validation) | **NOT AN ISSUE** | The MCP SDK validates `params.redirect_uri` against the registered client at `/authorize` before `pending_authorization` is stored. The stored value is server-side only, no path lets a client tamper with it between `/authorize` and `/callback`. Re-validation would be defensive double-check; not required for correctness. |
| CORS not configured | **NOT AN ISSUE** | Same-origin-only is the right policy here. Browsers enforce it; no JS in the wild needs cross-origin access to our endpoints. Adding explicit CORS would only loosen security. |
| Logging sensitive data | **NOT AN ISSUE** | Audited; passwords/MFA codes/tokens never appear in any log statement. The audit log only records event names + non-sensitive fields. |
| SQL parameterization | **NOT AN ISSUE** | Every SQL call in `storage.py` uses `?` placeholders. No string concatenation. Verified. |
| PKCE not enforced | **NOT AN ISSUE** | The DB schema has `code_challenge TEXT NOT NULL` on both `pending_authorizations` and `oauth_codes`. PKCE is enforced at the storage layer. |
| Refresh token rotation missing | **NOT AN ISSUE** | `provider.exchange_refresh_token` revokes the old token and issues a new pair atomically. Verified. |
| Volume mount permissions broken on first start | **NOT AN ISSUE** | Dockerfile pre-creates and chowns the mount targets to uid 1000; `Storage._init_schema` is idempotent. First boot works. |

---

## Open questions for review

1. **Track A1 — CSP `'unsafe-inline'` for styles.** OK to keep as-is
   (small attack surface — only the onboarding pages render
   inline `<style>`), or move the styles to a static CSS file
   served from `/static/` for a stricter CSP? Recommendation:
   keep inline; the onboarding HTML is server-generated and not
   a script-injection vector.

2. **Track A2 — bridge subnet pinning.** Hardcoding the Docker
   bridge subnet is fragile across hosts. Alternative: configure
   compose to use a fixed user-defined network (`networks:
   mcp_internal: { ipam: { config: [{ subnet: 172.30.0.0/16 }] }}`)
   so the subnet is deterministic. Recommendation: yes, do this in
   the same PR.

3. **Track A4 — keep the legacy root `Dockerfile` and
   `docker-compose.yml`?** They're upstream-stdio-mode leftovers.
   Recommendation: delete in this PR; document why in the commit
   message.

4. **Track B1 — `read_only: true` impact on uvicorn / Python.**
   Modern Python doesn't write to its install dir; bytecode goes
   to `__pycache__` next to the source files. With `read_only:
   true` we'd need to either bake bytecode into the image
   (`UV_COMPILE_BYTECODE=1` already on, line 17) or mount a tmpfs
   for cache. Verify by hand the first start doesn't 500.

5. **Track C1 — MFA timeout 90 s vs. status quo 5 min.** Real
   users sometimes wait for an SMS through a slow carrier. 90 s
   may be too tight for non-email-MFA cases. Alternative: keep
   the timeout, but evict the *session memory* aggressively — the
   thread can sit, but its row drops out of `self._sessions`
   sooner. Recommendation: 90 s timeout AND aggressive memory
   eviction; revisit if real users complain.

6. **Track C3 — kid headers and back-compat.** During the
   migration period (when both old un-kid'd tokens and new kid'd
   tokens are in flight), should the verifier accept tokens with
   no `kid` indefinitely or only for a configurable grace
   period? Recommendation: configurable grace, default 24 h
   after first use of `kid`, hard-fail no-kid tokens after.

---

## Suggested PR sequence

Same model as round 1: small focused PRs, CI must stay green
between them.

| # | Title | Track | Size |
|---|---|---|---|
| H1 | `harden: bump pygments via pytest minor + close pip-audit` | pre-flight | XS |
| H2 | `harden: security response headers in Caddy` | A1 | S |
| H3 | `harden: scope uvicorn forwarded_allow_ips, strip inbound XFF in Caddy` | A2 | M |
| H4 | `harden: wire RegistrationGuard.check_per_ip into /register` | A3 | M |
| H5 | `harden: pin all Docker base images by digest, drop legacy root Dockerfile` | A4 | S |
| H6 | `harden: drop capabilities + read-only rootfs + resource limits` | B1+B2 | S |
| H7 | `harden: /healthz verifies SQLite + disable Caddy admin API` | B3+B4 | S |
| H8 | `harden: bound onboarding session memory and worker lifetime` | C1 | S |
| H9 | `harden: fix cache double-load race + user-row UPSERT` | C2 | S |
| H10 | `harden: JWT signing key rotation via kid header` | C3 | M |
| H11 | `harden: CSRF tokens on /onboard POSTs` | C4 | M |
| H12 | `harden: bind onboarding tickets to UA + IP` | C5 | S |
| H13 | `harden: DCR allowlist for grant_types / response_types` | C6 | S |
| H14 | `harden: minor (Entra error sanitization, search len cap, doc note)` | D1+D2+D3 | XS |

XS ≈ 1–10 lines · S ≈ 10–50 lines · M ≈ 50–150 lines.

When all 14 land, delete this file and update
`doc/11-risks-and-technical-debt.md` (remove resolved items, leave
the deliberately-deferred ones with status updated).

---

## Addendum — three review passes (2026-05-10)

Round 2 PRs (H1–H14) plus two correction rounds (H15–H28) have been
reviewed. Status after all three passes:

| Item | Issue | Status |
|---|---|---|
| X1 | CSRF tokens on `/onboard` forms | **STILL NOT IMPLEMENTED** |
| X2a | UA binding checked in POST handlers | ✅ Fixed (H23) |
| X2b | IP binding checked in POST handlers | **STILL MISSING** |
| X2c | `OnboardingState.MFA` in `submit_mfa` line 237 | **RUNTIME CRASH BUG** |
| X3 | `evict_terminal_sessions()` wired into cleanup loop | ✅ Fixed (H17) |
| X4 | `/healthz` creates new `Storage` per call instead of using injected singleton | **STILL WRONG** |
| X5 | Caddyfile: `header -X-Forwarded-For` is wrong directive (response, not request) | **STILL WRONG** |
| X6 | Builder-stage images pinned by digest | ✅ Fixed (H26) |
| X7 | CI digest check (`check-digests.sh` + `ci.yml`) | ✅ Fixed (H26) |
| X8 | `cpus` + `pids` limits for both services | ✅ Fixed (H21) |
| X9 | Caddy `tmpfs` for `/tmp` | ✅ Fixed (H21) |
| X10 | `get_or_create_user` check-then-insert | accepted |
| X11 | Caddy service `memory` limit | ✅ Fixed (H27) |

**Remaining open: X1, X2b, X2c, X4, X5** — four items to fix in round 4.

---

## Round 4 — exact fix specifications

### H29 — CSRF tokens on `/onboard` forms (X1)

This is the **third time** this item is specified. The root cause
of previous failures: `csrf_token` was added to `OnboardingSession`
but `onboarding_routes.py` was **never touched**. The fix requires
changes in **both** files.

**Verified current state.**
- `onboarding.py:70` — `csrf_token` field exists in `OnboardingSession` ✓
- `onboarding_routes.py` — zero references to `csrf_token`, `hmac`,
  `set_cookie`, or `__Host-`. No changes were made to this file in H22.

**Step 1 — `onboarding_routes.py`: add `hmac` import**

At the top of the file, add:
```python
import hmac
```

**Step 2 — `onboarding_routes.py`: pass `csrf_token` into `_credentials_form`**

Change signature:
```python
def _credentials_form(ticket: str, csrf_token: str, error: str | None = None) -> str:
```

Inside the form HTML, after the ticket hidden input, add:
```python
  <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
```

**Step 3 — `onboarding_routes.py`: pass `csrf_token` into `_panel_html`**

Change signature:
```python
def _panel_html(state: OnboardingState, ticket: str, csrf_token: str, error: str | None = None) -> str:
```

In the `NEW` branch, pass it to `_credentials_form`:
```python
body = _credentials_form(ticket, csrf_token, error)
```

In the `AWAITING_MFA` branch, add the hidden input after the ticket
input:
```python
  <input type="hidden" name="csrf_token" value="{html.escape(csrf_token)}">
```

**Step 4 — `onboarding_routes.py`: `onboard_page` — set cookie, pass token**

Replace the current `onboard_page` return statement:
```python
# BEFORE:
page = _layout(_panel_html(session.state, ticket, session.error_message))
return HTMLResponse(page)

# AFTER:
page = _layout(_panel_html(session.state, ticket, session.csrf_token, session.error_message))
response = HTMLResponse(page)
response.set_cookie(
    "__Host-csrf",
    session.csrf_token,
    httponly=True,
    secure=True,
    samesite="strict",
    path="/",
)
return response
```

All other `_panel_html` call sites (`status` handler) must also pass
`session.csrf_token` as the third argument.

**Step 5 — `onboarding_routes.py`: verify token in `submit_credentials`**

After the existing field-presence check and before `manager.submit_credentials`,
insert:
```python
posted_csrf = (form.get("csrf_token") or "").strip()
session_pre = manager.get(ticket)
if session_pre is None or not hmac.compare_digest(
    posted_csrf.encode(), session_pre.csrf_token.encode()
):
    return HTMLResponse(
        _panel_html(OnboardingState.NEW, ticket, session_pre.csrf_token if session_pre else "", "Invalid or missing CSRF token."),
        status_code=400,
    )
```

**Step 6 — `onboarding_routes.py`: verify token in `submit_mfa`**

After reading `ticket` and `code` from the form, and before the UA
binding check, insert:
```python
posted_csrf = (form.get("csrf_token") or "").strip()
session_csrf = manager.get(ticket)
if session_csrf is None or not hmac.compare_digest(
    posted_csrf.encode(), session_csrf.csrf_token.encode()
):
    return HTMLResponse(
        _panel_html(OnboardingState.AWAITING_MFA, ticket, session_csrf.csrf_token if session_csrf else "", "Invalid or missing CSRF token."),
        status_code=400,
    )
```

**Verification:**
```bash
grep -n "csrf_token\|__Host-csrf\|hmac" src/garmin_mcp/auth/onboarding_routes.py
# Must return multiple hits in _credentials_form, _panel_html, onboard_page,
# submit_credentials, and submit_mfa.
```

---

### H30 — Fix IP binding check + `OnboardingState.MFA` crash (X2b + X2c)

**Two bugs in `onboarding_routes.py`:**

**Bug 1 — `OnboardingState.MFA` does not exist (line 237, runtime crash).**

The enum in `onboarding.py` has: `NEW`, `AUTHENTICATING`, `AWAITING_MFA`,
`COMPLETE`, `FAILED`, `EXPIRED`. There is no `MFA`.

`submit_mfa` line 237 currently:
```python
                        OnboardingState.MFA,
```
Replace with:
```python
                        OnboardingState.AWAITING_MFA,
```

This is a one-word fix. Missing it causes `AttributeError` on any UA
mismatch during the MFA step.

**Bug 2 — IP binding checked for UA but not for IP.**

Both `submit_credentials` and `submit_mfa` check `session.user_agent_hash`
but never check `session.client_ip`. After the UA hash check in each
handler, add the IP check:

```python
# After the UA hash check block, add:
if session is not None and session.client_ip is not None:
    incoming_ip = request.client.host if request.client else ""
    if incoming_ip != session.client_ip:
        return HTMLResponse(
            _panel_html(
                OnboardingState.NEW,   # or AWAITING_MFA in submit_mfa
                ticket,
                session.csrf_token,
                "Session binding mismatch — please restart onboarding.",
            ),
            status_code=403,
        )
```

**Verification:**
```bash
grep -n "client_ip\|client\.host" src/garmin_mcp/auth/onboarding_routes.py
# Must find hits in submit_credentials and submit_mfa.

python -c "from garmin_mcp.auth.onboarding import OnboardingState; print(OnboardingState.AWAITING_MFA)"
# Must not raise AttributeError.
```

---

### H31 — Fix `/healthz` to use the injected `Storage` instance (X4)

**Current state** (`server.py:126–143`): creates a brand-new
`Storage(db_path)` on every call. This is still an architecture
violation — it opens a second SQLite connection that bypasses the
production singleton, and creates an empty DB file if the path doesn't
exist yet.

**`Storage.close()` exists** (line 134 of `storage.py`), so the
finally block is correct — but it should never need to be called
because `healthz` should not own a storage instance at all.

**The fix: make `healthz` a closure inside `make_app`.**

In `make_app()` in `server.py`, locate where `storage` is created or
accepted as a parameter. Define `healthz` as a nested function there:

```python
def make_app(
    ...
    storage: Storage | None = None,
    ...
):
    _storage = storage  # capture in closure

    async def healthz(_: Request) -> JSONResponse:
        if _storage is None:
            # stdio / single-user mode — no DB to check
            return JSONResponse({"status": "ok"})
        try:
            await asyncio.to_thread(_storage.count_clients)
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            logging.getLogger(__name__).warning("healthz db check failed: %s", exc)
            return JSONResponse({"status": "fail"}, status_code=503)

    ...
    routes = [Route("/healthz", healthz), ...]
```

Delete the module-level `healthz` function (lines 126–143) entirely.
`asyncio` and `logging` are already imported at module level.

**Verification:**
```bash
grep -n "sqlite3\|Storage(" src/garmin_mcp/server.py | grep -i health
# Must return no output — healthz must not instantiate Storage itself.

grep -n "count_clients" src/garmin_mcp/server.py
# Must find it inside the healthz closure.
```

---

### H32 — Fix Caddyfile XFF directive (X5)

**Third attempt.** History of failures:
- Round 1: `request_header X-Forwarded-For {remote_host}` then `request_header -X-Forwarded-For` — set then immediately deleted.
- Round 2 (H19): replaced with `header_down -X-Forwarded-For` — strips response headers, not requests.
- Round 3 (H25): replaced with `header -X-Forwarded-For` — in Caddy v2 the bare `header` directive in a site block also modifies **response** headers. Still wrong.

**The only correct directive to strip an incoming request header in
Caddy v2 is `request_header -<field>`.**

`deploy/Caddyfile` line 50 currently reads:
```caddy
    header -X-Forwarded-For
```

**Replace that one line with:**
```caddy
    request_header -X-Forwarded-For
```

The full block after the fix must look exactly like this — do not
change anything else:

```caddy
    request_header X-Forwarded-Proto {scheme}
    request_header X-Real-IP        {remote_host}

    # Strip any client-supplied X-Forwarded-For from incoming requests.
    request_header -X-Forwarded-For
    reverse_proxy garmin-mcp:8000 {
        header_up X-Forwarded-For {remote_host}
        flush_interval -1
    }
```

**Verification — the only acceptable grep output:**
```bash
grep "X-Forwarded-For" deploy/Caddyfile
#   request_header -X-Forwarded-For
#       header_up X-Forwarded-For {remote_host}
```
If the output contains `header_down` or bare `header`, the fix is wrong.

---

### Round 4 verification checklist

Run after all four PRs are merged:

```bash
# X1 — CSRF implemented in routes
grep -c "csrf_token" src/garmin_mcp/auth/onboarding_routes.py
# Expected: ≥ 6 (form fields, cookie, verifications)

grep -c "__Host-csrf" src/garmin_mcp/auth/onboarding_routes.py
# Expected: ≥ 1

grep -c "hmac.compare_digest" src/garmin_mcp/auth/onboarding_routes.py
# Expected: 2 (one per POST handler)

# X2c — invalid enum value gone
grep "OnboardingState\.MFA" src/garmin_mcp/auth/onboarding_routes.py
# Expected: no output

# X2b — IP check present
grep "client_ip" src/garmin_mcp/auth/onboarding_routes.py
# Expected: ≥ 2 hits (submit_credentials + submit_mfa)

# X4 — no standalone Storage instantiation in healthz
grep -n "Storage(" src/garmin_mcp/server.py
# Expected: zero hits OR only in make_production_app, never in healthz

# X5 — correct Caddy directive
grep "X-Forwarded-For" deploy/Caddyfile
# Expected: exactly two lines:
#   request_header -X-Forwarded-For
#       header_up X-Forwarded-For {remote_host}

# Full test suite still green
uv run pytest tests/unit/ tests/integration/ -q
# Expected: all pass
```
