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

Each item below was a deviation from a requirement in
[chapter 8](08-crosscutting-concepts.md). All items have been resolved
via PRs H1–H28 (round 2 + corrections) and H29–H32 (addendum), now all merged.

### R11 — Web security headers not set on browser-facing responses

**Status: ✅ CLOSED (H2, PR #40).**

Web security headers (HSTS, CSP, X-Frame-Options, X-Content-Type-Options,
Referrer-Policy) are now set in Caddy and applied to all responses.

### R12 — Forwarded-IP trust over-broad

**Status: ✅ CLOSED (H3, PR #41 + H32, PR #65).**

`forwarded_allow_ips` now scoped to `127.0.0.1` (the Docker bridge);
Caddy explicitly strips inbound `X-Forwarded-For` before forwarding.

### R13 — `/register` per-IP rate limit not wired

**Status: ✅ CLOSED (H4, PR #42).**

`RegistrationGuard.check_per_ip()` is now called in `provider.register_client()`,
enforcing the documented "5 successful DCR registrations / IP / hour" cap.

### R14 — Base images pinned by tag, not digest

**Status: ✅ CLOSED (H5, PR #43 + H26, PR #60).**

All Docker base image `FROM` lines pinned by digest; CI check prevents regression.

### R15 — Container capabilities + read-only rootfs not set

**Status: ✅ CLOSED (H6, PR #44).**

Both containers run with `cap_drop: ALL`, `no-new-privileges: true`,
and read-only root filesystem (with necessary tmpfs mounts).

### R16 — No container resource limits

**Status: ✅ CLOSED (H6, PR #44 + H21, PR #56).**

Both services have CPU, memory, and PID limits set in compose; Caddy
has tmpfs size cap for `/tmp`.

### R17 — `/healthz` doesn't verify the database

**Status: ✅ CLOSED (H7, PR #45 + H31, PR #64).**

`/healthz` now queries the SQLite database; uses the injected Storage
singleton to avoid fd leaks.

### R18 — Caddy admin API not explicitly disabled

**Status: ✅ CLOSED (H7, PR #45).**

Caddy admin API disabled via `admin off` in the global config.

### R19 — Onboarding session and worker-thread leaks

**Status: ✅ CLOSED (H8, PR #46 + H17, PR #57).**

`OnboardingManager.evict_terminal_sessions()` wired into cleanup loop;
sessions capped at 10 concurrent, expired sessions cleaned up.

### R20 — Cache double-load + user-row insert races

**Status: ✅ CLOSED (H9, PR #47).**

Cache loading protected by double-checked locking; user-row inserted via
`INSERT OR IGNORE` (atomic, no race on UNIQUE constraint).

### R21 — JWT signing key has no `kid`-based rotation

**Status: ✅ CLOSED (H10, PR #48).**

JWT tokens now carry a `kid` (key ID) header; verifier can accept both
old and new signing keys during rotation with configurable grace period.

### R22 — No CSRF protection on `/onboard` POSTs

**Status: ✅ CLOSED (H11, PR #49 + H29, PR #62).**

`OnboardingSession` carries a random CSRF token; all POST handlers verify
via `hmac.compare_digest` before processing.

### R23 — Onboarding tickets not bound to UA + IP

**Status: ✅ CLOSED (H12, PR #53 + H23, PR #61 + H30, PR #63).**

Tickets bound to User-Agent hash and client IP at OAuth callback;
POST handlers verify both bindings before accepting credentials/MFA.

### R24 — DCR doesn't allowlist `grant_types` / `response_types`

**Status: ✅ CLOSED (H13, PR #51).**

Registration now rejects clients with disallowed grant/response types;
only `authorization_code` (with PKCE) is accepted.
