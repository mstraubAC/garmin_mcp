# 11. Risks and technical debt

Living list — items get added as we find them, removed as we ship
fixes. Each entry: what + impact + mitigation status.

The detailed PR-by-PR plan for closing the round-2 entries lives in
[`/HARDENING.md`](../HARDENING.md) at the project root and is deleted
once executed; this chapter is the durable record.

## Carried forward from round 1

### R1 — Single VPS = single point of failure

**Impact.** VPS host outage takes the whole service down.

**Status: partially mitigated.** Off-site backup (`deploy/backup-offsite.sh`,
PR #22) brings recovery from "catastrophic data loss" down to "rebuild
on a fresh VPS within ~1 hour." The SPOF itself remains accepted at
personal scale. Long-term path to HA: Postgres backend + load-balanced
pair; not warranted yet.

### R3 — MFA UX is brittle

**Impact.** garth's MFA flow blocks a worker thread until the user
fetches the code from email. A server restart mid-onboarding loses the
session.

**Status: accepted.** Session lifetime is bounded; the user is told
clearly when it expires. Restart-safety would require persisting the
worker state to SQLite, which the round-2 audit didn't push us toward
yet.

### R4 — Bicep / Entra app-registration drift

**Impact.** The Microsoft Graph Bicep extension is preview. If Microsoft
changes the schema or pulls the extension, our `infra/azure/main.bicep`
may stop deploying.

**Status: partially mitigated.** Bicep validation in CI (PR #12) catches
schema breaks on every PR before production. README also documents a
fallback path using raw `az ad app create`. The underlying preview-API
risk is accepted.

### R10 — Schema migrations forward-compat-only

**Impact.** We can never *change* a column or *drop* a table without a
real migration tool.

**Status: accepted.** Live with it; introduce a proper migration framework
(alembic-style) at the point we genuinely need to alter a column.
Premature now.

---

## Round 2 — gaps surfaced by the post-round-1 audit

Each item below is a deviation from a requirement in
[chapter 8](08-crosscutting-concepts.md). See
[`/HARDENING.md`](../HARDENING.md) for the PR plan.

### R11 — Web security headers not set on browser-facing responses

**Impact.** `/onboard*` returns HTML without HSTS / CSP / X-Frame-Options /
X-Content-Type-Options / Referrer-Policy. An XSS or clickjacking
vulnerability in the onboarding form has no defense in depth.

**Required by:** [§ Web security headers](08-crosscutting-concepts.md#web-security-headers).
**Plan:** HARDENING A1 (PR H2).

### R12 — Forwarded-IP trust over-broad

**Impact.** uvicorn `forwarded_allow_ips="*"` lets any sender that reaches
:8000 spoof the source IP via `X-Forwarded-For`. With Caddy in front
this is contained, but a misconfig that exposes :8000 directly turns the
per-IP guard (R13) into a no-op.

**Required by:** [§ Trust boundary](08-crosscutting-concepts.md#trust-boundary).
**Plan:** HARDENING A2 (PR H3).

### R13 — `/register` per-IP rate limit not wired

**Impact.** `RegistrationGuard.check_per_ip()` is implemented + unit-
tested but never called from `provider.register_client()`. The
documented "5 successful DCR registrations / IP / hour" cap is
documentation, not behavior. Up to the global cap (10 000 rows), DCR is
unrate-limited per source.

**Required by:** [§ Rate limiting](08-crosscutting-concepts.md#rate-limiting).
**Plan:** HARDENING A3 (PR H4).

### R14 — Base images pinned by tag, not digest

**Impact.** A future `python:3.13-slim` rebuild upstream — or a
compromised registry push — silently changes our runtime contents on
the next `docker compose build`.

**Required by:** [§ Supply chain integrity](08-crosscutting-concepts.md#supply-chain-integrity).
**Plan:** HARDENING A4 (PR H5).

### R15 — Container capabilities + read-only rootfs not set

**Impact.** Both containers run with the default Linux capability set
(CAP_NET_RAW, CAP_DAC_OVERRIDE, …) and writable root filesystems. RCE in
either container has more leverage than necessary.

**Required by:** [§ Container hardening](08-crosscutting-concepts.md#container-hardening).
**Plan:** HARDENING B1 (PR H6).

### R16 — No container resource limits

**Impact.** A runaway tool call or memory leak can OOM the host. On a
small VPS, the kernel's victim is often `dockerd` itself.

**Required by:** [§ Container hardening](08-crosscutting-concepts.md#container-hardening).
**Plan:** HARDENING B2 (PR H6).

### R17 — `/healthz` doesn't verify the database

**Impact.** A corrupted SQLite, locked file, or permission failure
keeps the container in "healthy" state. Docker doesn't restart it,
Caddy keeps proxying, requests fail with 500s.

**Required by:** [§ Operational health](08-crosscutting-concepts.md#operational-health).
**Plan:** HARDENING B3 (PR H7).

### R18 — Caddy admin API not explicitly disabled

**Impact.** Caddy's default admin API (localhost:2019) isn't bound
externally but is reachable from any process inside the caddy
container. RCE in caddy → reload routes / dump config / shut Caddy
down.

**Required by:** [§ Container hardening](08-crosscutting-concepts.md#container-hardening).
**Plan:** HARDENING B4 (PR H7).

### R19 — Onboarding session and worker-thread leaks

**Impact.** Terminal onboarding sessions (COMPLETE / FAILED / EXPIRED)
are never evicted from `OnboardingManager._sessions` until a new
`create_session()` runs. The MFA timeout is 5 minutes, so each abandoned
session holds a daemon thread for that long. Sustained probing creates
many concurrent threads + entries.

**Required by:** [§ Concurrency model](08-crosscutting-concepts.md#concurrency-model).
**Plan:** HARDENING C1 (PR H8).

### R20 — Cache double-load + user-row insert races

**Impact.** Concurrent first-call requests for the same `user_id`
both miss the cache and both decrypt + build a `Garmin` instance.
Concurrent first-sign-ins for the same `(entra_sub, entra_tid)` race
on the INSERT and one raises a UNIQUE constraint exception.

**Required by:** [§ Concurrency model](08-crosscutting-concepts.md#concurrency-model).
**Plan:** HARDENING C2 (PR H9).

### R21 — JWT signing key has no `kid`-based rotation

**Impact.** Rotating `JWT_SIGNING_KEY` instantly invalidates every
in-flight access token. Every connected Claude session has to refresh.
Works, but isn't graceful — and discourages key rotation.

**Required by:** [§ Authentication and authorization](08-crosscutting-concepts.md#authentication-and-authorization).
**Plan:** HARDENING C3 (PR H10).

### R22 — No CSRF protection on `/onboard` POSTs

**Impact.** A malicious page that knows or guesses an active onboarding
ticket can submit on the user's behalf. Window is small (5-min ticket
TTL) but the consequence is the user's Garmin password is delivered
to the attacker.

**Required by:** [§ CSRF protection](08-crosscutting-concepts.md#csrf-protection).
**Plan:** HARDENING C4 (PR H11).

### R23 — Onboarding tickets not bound to UA + IP

**Impact.** A ticket leaked via Referer / browser history / log files
is replayable from any device for ~5 minutes.

**Required by:** [§ Multi-user isolation](08-crosscutting-concepts.md#multi-user-isolation).
**Plan:** HARDENING C5 (PR H12).

### R24 — DCR doesn't allowlist `grant_types` / `response_types`

**Impact.** A client could register with `grant_types=["password"]` or
`response_types=["token"]`. The SDK rejects those flows at `/token`,
but the registration itself shouldn't be accepted in the first place.

**Required by:** [§ Authentication and authorization](08-crosscutting-concepts.md#authentication-and-authorization).
**Plan:** HARDENING C6 (PR H13).
