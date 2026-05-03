# 11. Risks and technical debt

Living list — items get added as we find them, removed as we ship fixes.
Each entry: what + impact + planned mitigation.

## R1 — Single VPS = single point of failure

**Impact.** VPS host outage takes the whole service down. Personal-scale
users get nothing while the host is unreachable.

**Mitigation, accepted for now.** Backup script lets us rebuild on a new
host within an hour. Step 9 will add an off-site (S3 / B2) backup target
so a disk failure isn't terminal. Long-term path to HA: Postgres
backend + load-balanced compose pair behind shared storage. Not
warranted at current scale.

## R2 — Garmin token expiry isn't auto-detected

**Impact.** When a user's `garth` tokens expire (~6 months), the next
tool call returns a Garmin 401 deep inside the tool code. Right now
that surfaces as a generic error; the user has to know to visit
`/onboard` again to re-authenticate.

**Mitigation.** Catch 401s in the cache layer, invalidate the cached
client, and surface a structured error to the MCP client telling the
user to re-onboard. Tracked in step 9. Helper hook design: an MCP
"resource" exposing "Re-authenticate at <url>" so Claude can surface
it in the chat naturally.

## R3 — MFA UX is brittle

**Impact.** garth's MFA flow blocks a worker thread for up to 5 min
waiting for the user to fetch the code from email. If the worker
crashes or the server restarts mid-onboarding, the session is lost
and the user starts over.

**Mitigation.** The session lifetime is bounded (5 min) and the user
is told clearly when it expires. Restart-safety is not free —
persisting the worker state to SQLite would require re-engineering
the worker loop. Accepted as a known limitation.

## R4 — Bicep / Entra app-registration drift

**Impact.** The Microsoft Graph Bicep extension is preview as of late
2025. If Microsoft changes the schema or pulls the extension, our
`infra/azure/main.bicep` may stop deploying.

**Mitigation.** README documents a fallback path using raw `az ad app
create` commands. Re-running `bicep build main.bicep` weekly in CI
would catch breakage early — not yet wired up.

## R5 — `GARMIN_MCP_DATA_KEY` rotation has no automated path

**Impact.** Rotating the Fernet key requires re-encrypting every
`garmin_tokens` row with the new key. Currently done by hand: read
with old key, write with new key, swap env var, restart. No tooling.

**Mitigation.** Step 9 will add a `rotate-data-key` CLI subcommand that
takes an old + new key, walks the table, and re-encrypts. Until then,
keep the existing key safe — there's no good reason to rotate it
proactively.

## R6 — All users share the VPS's outbound IP at Garmin

**Impact.** Garmin rate-limits per source IP. If one user is hammering
their Garmin account (e.g. an LLM agent in a tight loop) it will
trigger Garmin's rate limiter for *everyone* on the same VPS.

**Mitigation.** Step 9 will add per-user app-side rate limiting on
tool calls (token bucket per user_id, e.g. 60 calls/min). Until then,
this risk is real for multi-user deploys; for single-user it's a
non-issue.

## R7 — Audit log isn't tamper-evident

**Impact.** A compromised root account could rewrite past audit
entries. The current append-only-by-convention log gives forensic
visibility but not non-repudiation.

**Mitigation, deferred.** Hash chaining or shipping logs to an
external sink would solve it. Not justified at personal scale; noted
here for completeness.

## R8 — No CI yet

**Impact.** Test breakage from upstream package updates (mcp SDK,
garth, etc.) won't be caught until someone runs the suite locally.

**Mitigation.** Step 9 includes adding a GitHub Actions workflow
running `uv sync` + `uv run pytest` on push and PR.

## R9 — htmx CDN dependency

**Impact.** The onboarding page loads htmx from `unpkg.com`. If unpkg
is unreachable from the user's network at the moment they're onboarding,
the polling-based MFA flow breaks.

**Mitigation.** Vendor htmx into the static assets directory and serve
it locally. Ten minutes of work; deferred until we hit the issue
once.

## R10 — Schema migrations forward-compat-only constraint

**Impact.** We can never *change* a column or *drop* a table without
a real migration tool. Current rule "add tables only" works for now
but will eventually be limiting.

**Mitigation.** Live with it; when we genuinely need to change a
column, introduce a proper migration framework (alembic-style) at
that point. Premature now.
