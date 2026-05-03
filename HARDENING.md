# Hardening plan

This document is the detailed plan for the "hardening" step of the
multi-user/Entra rollout — closing the loose ends in
[`doc/11-risks-and-technical-debt.md`](doc/11-risks-and-technical-debt.md)
and the gaps surfaced while wiring CI.

It's organized as **five tracks**, each shippable as its own PR (or a
small group of PRs). Tracks are roughly ordered by safety value: do
the CI + dependency CVE work first so the rest can land green; do the
operational tools (key rotation, off-site backup) before they're
needed in anger.

Each item carries:

- **Why** — the risk or gap it closes (links to chapter 11 or to a
  freshly-discovered issue)
- **What** — the concrete code/config change
- **Files** — what this PR touches
- **Tests** — how the change is verified
- **Size** — rough estimate so PRs stay reviewable

When this plan is fully executed, **`HARDENING.md` should be deleted**;
the lasting record lives in `doc/11-risks-and-technical-debt.md`
(items removed) and the new ADRs in `doc/09-architecture-decisions.md`
(items added).

---

## Pre-flight finding (block everything else)

`pip-audit` against the current `uv.lock` (May 2026) reports 8 CVEs:

| Package | Version | Fix | Notes |
|---|---|---|---|
| cryptography | 46.0.5 | 46.0.7 | Direct dep via Fernet + Entra OIDC validation |
| requests | 2.32.4 | 2.33.0 | Pinned in `pyproject.toml` |
| pyjwt | 2.11.0 | 2.12.0 | We use this for our own JWT signing |
| python-dotenv | 1.0.1 | 1.2.2 | Pinned in `pyproject.toml` |
| python-multipart | 0.0.22 | 0.0.26 | Used by Starlette form parsing in `/onboard` |
| pygments | 2.19.2 | 2.20.0 | Transitive |
| pytest | 9.0.2 | 9.0.3 | Dev only |

Wherever a track below adds `pip-audit` to CI, the dependency bumps
must land in the same PR — otherwise the CI job goes red on its first
run.

---

## Track 1 — CI hardening

Fix bugs in the inherited workflows and add the validation jobs that
should already be there for the surface this fork added (Bicep,
shell scripts, encrypted-config Fernet keys, doc/ links).

Closes: **R8** (chapter 11) — partially, since CI exists in the upstream
but doesn't actually validate everything we ship.

### 1.1 Fix `py_compile` recursion bug in `security.yml`

**Why.** `code-quality` job runs
`uv run python -m py_compile src/garmin_mcp/*.py`, which only
glob-matches the top level. Everything under `auth/`,
`maintenance/`, and any future submodule is **not** syntax-checked.

**What.** Replace with `uv run python -m compileall src/garmin_mcp -q`
which recurses correctly.

**Files.** `.github/workflows/security.yml`

**Tests.** Trigger the workflow on a branch that intentionally
introduces a syntax error in `auth/jwt.py` — confirm the job fails.

**Size.** 1 line.

### 1.2 Wire real `pip-audit` (with the dep bumps)

**Why.** `security.yml` has `# Note: Add pip-audit or safety scan here
if desired` as a TODO. Running it locally surfaces 8 real CVEs.

**What.**
1. Bump pinned deps in `pyproject.toml`:
   - `requests==2.32.4` → `requests>=2.33.0,<3`
   - `python-dotenv==1.0.1` → `python-dotenv>=1.2.2,<2`
2. `uv lock` to refresh transitives (`cryptography`, `pyjwt`,
   `python-multipart`, `pygments`).
3. Bump `pytest>=9.0.3` in `[tool.uv.dev-dependencies]`.
4. Add a `pip-audit` step to `security.yml`'s `dependency-check` job,
   running `uvx pip-audit --strict` against an exported requirements
   file. Job fails on any unfixed CVE.

**Files.** `pyproject.toml`, `uv.lock`, `.github/workflows/security.yml`

**Tests.** `uv run pytest` locally (must stay green after the bump);
`uvx pip-audit` should report 0 vulns.

**Size.** ~30 lines across 3 files.

### 1.3 Add Bicep validation workflow

**Why.** We ship `infra/azure/` but nothing checks the Bicep on PRs.
A typo in `main.bicep` would only be caught when the operator runs
`scripts/deploy.sh`.

**What.** New `.github/workflows/infra.yml` with one job that:
1. `actions/checkout@v4`
2. Installs Azure CLI (`az`) on `ubuntu-latest` — which already has
   it preinstalled.
3. `az bicep upgrade` to ensure recent CLI.
4. Runs `bicep build infra/azure/main.bicep --stdout > /dev/null`.
5. Runs `bicep build-params infra/azure/parameters/*.bicepparam --stdout
   > /dev/null` for each.

Trigger: PRs touching `infra/azure/**` (use `paths:` filter).

**Files.** `.github/workflows/infra.yml` (new)

**Tests.** Force-push a broken `.bicep` file; confirm job fails.

**Size.** ~40 lines.

### 1.4 Add bash syntax-check workflow

**Why.** We ship `deploy/backup.sh`, `infra/azure/scripts/deploy.sh`,
`infra/azure/scripts/rotate-secret.sh`. A `set -e` typo or unclosed
quote isn't caught until someone runs the script in anger.

**What.** Same `infra.yml` (or a new `scripts.yml`) with a job that
runs `bash -n` and `shellcheck` on every `*.sh` under `deploy/` and
`infra/`. shellcheck is preinstalled on ubuntu-latest.

**Files.** `.github/workflows/infra.yml` (extend) or
`.github/workflows/scripts.yml` (new)

**Tests.** Introduce a syntactically-broken script on a branch;
confirm the job fails.

**Size.** ~20 lines.

### 1.5 Add markdown link checker

**Why.** AGENTS.md and the README explicitly say "doc/ updates are
part of the diff". A broken `[text](path)` link in a doc PR would
slip through.

**What.** Use `lycheeverse/lychee-action@v2` to walk every `*.md`
under `README.md`, `AGENTS.md`, `doc/`, `deploy/`, `infra/` and
verify all relative-path links resolve to files that exist. External
HTTP links: ignored (too noisy / rate-limited). Run on PRs touching
markdown.

**Files.** `.github/workflows/docs.yml` (new) or fold into existing
`security.yml`'s code-quality job

**Tests.** Introduce a `[broken](nonexistent.md)` link on a branch;
confirm the job fails.

**Size.** ~30 lines.

### 1.6 Consolidate or document overlap between `ci.yml` and `pr-validation.yml`

**Why.** `pr-validation.yml`'s `validate` job repeats what `ci.yml`
already does (with one matrix slot vs all four). This wastes minutes
and confuses contributors who wonder which one matters.

**What.** **Decision needed** — one of:
- (a) Delete `pr-validation.yml` entirely; rely on `ci.yml` matrix.
  Simpler, but loses the `test-installation` and `pr-info` jobs.
- (b) Strip `pr-validation.yml` down to just `test-installation` +
  `pr-info`; let `ci.yml` own the actual test runs.
- (c) Leave alone, document the overlap in `WORKFLOWS.md`.

Recommendation: (b). The `test-installation` job (`uv run garmin-mcp
--help`) is genuinely useful and not in `ci.yml`.

**Files.** `.github/workflows/pr-validation.yml`,
`.github/WORKFLOWS.md`

**Tests.** Push to a branch; confirm both workflows still green.

**Size.** ~40 lines removed, 5 added.

### 1.7 Add Python 3.14 to the matrix

**Why.** Local dev uses 3.14 (per `pyproject.toml: requires-python =
">=3.10"` — 3.14 is in range). The matrix only covers 3.10–3.13.

**What.** Add `'3.14'` to the matrix in `ci.yml`. Verify the suite
still passes there (it does locally as of May 2026).

**Files.** `.github/workflows/ci.yml`

**Tests.** CI on the PR.

**Size.** 1 line.

### Track 1 PR boundary suggestion

- **PR 9a:** items 1.1 + 1.2 + 1.7 — bug fix, dep bumps, real CVE
  scanning, Python 3.14. Single coherent "make CI tell the truth" PR.
- **PR 9b:** items 1.3 + 1.4 — infra + scripts validation. Self-
  contained.
- **PR 9c:** item 1.5 — link checker.
- **PR 9d:** item 1.6 — workflow consolidation. Smallest, can wait.

---

## Track 2 — Application hardening

Things the *running* server does (or doesn't) that affect security
and reliability.

### 2.1 Per-user tool-call rate limit

**Why.** Closes **R6** (chapter 11). Right now any one user can
hammer Garmin from inside a Claude conversation; Garmin's per-IP
limiter then trips for every other user on the VPS.

**What.** Reuse the existing `TokenBucket` from `auth/throttle.py` to
build a `ToolCallGuard`. Two layers:
1. **Per-user** bucket: e.g. capacity=60, refill=60/60s
2. **Global outbound** bucket: e.g. capacity=120, refill=120/60s
   (gives Garmin breathing room even if all users are active)

Wire the guard inside `MultiUserClientCache.get_or_load` (after
resolution): wrap each method invocation on the returned `Garmin`
instance with a check, OR — cleaner — wrap the FastMCP tool dispatch
with an async middleware that consults the guard before calling the
tool.

The middleware approach is preferred: tools stay clean (per ADR
convention "tools never see auth"), and the rate-limit behavior is
in one place.

**Files.**
- `src/garmin_mcp/auth/throttle.py` — extend with `ToolCallGuard`
- `src/garmin_mcp/server.py` — wire the middleware into the Starlette
  stack (BEFORE the FastMCP mount, AFTER the JWT verifier so we have
  `current_user_id()`)
- `src/garmin_mcp/auth/storage.py` — already has `rate_limit_buckets`
  table; reuse it
- `tests/unit/test_auth_throttle.py` — add test cases for
  `ToolCallGuard`
- `tests/integration/test_oauth_flow.py` — add a test that hammers
  one user past the limit and verifies a 429-ish response
- `doc/08-crosscutting-concepts.md` — update the "Rate limiting"
  section, remove the "no per-user limit on tool calls (yet)" note
- `doc/11-risks-and-technical-debt.md` — remove R6

**Tests.** Per-user limit triggers at the right count; one user
hitting the limit doesn't affect another user; global outbound limit
also triggers.

**Size.** Medium — ~150 lines including tests + doc.

### 2.2 Auto-detect expired Garmin tokens

**Why.** Closes **R2** (chapter 11). garth tokens last ~6 months;
when they expire, the next tool call returns a Garmin 401 buried
inside the tool's exception. The user has to know to visit
`/onboard`.

**What.** Catch authentication errors in the cache layer:

1. Wrap every method invocation on the cached `Garmin` instance with
   an exception filter that catches `garth.exc.GarthHTTPError` /
   `GarminConnectAuthenticationError` with a 401 status.
2. On match: invalidate the user's cached entry, raise a domain
   exception `GarminSessionExpiredError(user_id)`.
3. Add an MCP-level error handler that turns this into a structured
   JSON-RPC error containing the URL the user should visit:
   `{onboarding_url: "https://.../onboard?ticket=..."}`. Issue the
   ticket via `OnboardingManager.create_session(user_id)` so the
   on_success callback resumes whatever they were trying to do —
   actually no, we don't have a pending OAuth here, this is a tool
   call mid-session. Simpler: issue the ticket without an
   `on_success`; the user re-onboards and their next tool call
   succeeds because the cache reloads from the new token blob.

**Files.**
- `src/garmin_mcp/user_context.py` — wrap `MultiUserClientCache` so
  loaded clients have a "401 → invalidate + raise" wrapper around
  every method
- `src/garmin_mcp/auth/onboarding.py` — `create_session` already
  works without an `on_success`; verify
- `src/garmin_mcp/server.py` — error middleware that turns
  `GarminSessionExpiredError` into a JSON-RPC error
- `tests/unit/test_user_context.py` — test that 401 invalidates the
  cache + raises
- `tests/integration/test_onboarding_flow.py` — flow test:
  authenticated user → tool call → simulated 401 → expected error +
  fresh onboarding ticket
- `doc/06-runtime-view.md` — flesh out the "Garmin token refresh"
  section (currently a stub)
- `doc/11-risks-and-technical-debt.md` — remove R2

**Tests.** Mock `Garmin.get_full_name` to raise a 401; verify cache
gets invalidated; verify the error response contains the onboarding
URL.

**Size.** Medium — ~120 lines.

### 2.3 Vendor htmx (drop CDN dependency)

**Why.** Closes **R9** (chapter 11). The onboarding page loads htmx
from `unpkg.com`. If unpkg is unreachable from the user's network at
the moment they're onboarding, the polling MFA UI breaks.

**What.**
1. Download `htmx.min.js` (~17 KB) into `src/garmin_mcp/auth/static/`.
2. Mount a Starlette `StaticFiles` route at `/static` in `server.py`.
3. Update `auth/onboarding_routes.py` to point the `<script>` tag at
   `/static/htmx.min.js` instead of unpkg.

Pin the htmx version (e.g. 2.0.4 — already in the inline tag).
Document refresh procedure.

**Files.**
- `src/garmin_mcp/auth/static/htmx.min.js` (new)
- `src/garmin_mcp/auth/onboarding_routes.py` — drop `HTMX_SCRIPT`
  CDN URL, point at `/static/...`
- `src/garmin_mcp/server.py` — mount StaticFiles
- `pyproject.toml` — `[tool.hatch.build]` may need an `include`
  glob if hatchling doesn't pick up the `.js` automatically
- `doc/11-risks-and-technical-debt.md` — remove R9

**Tests.** Existing onboarding integration tests cover the form
rendering; add an explicit GET `/static/htmx.min.js` returns 200
test.

**Size.** Small — ~40 lines + 1 vendored file.

---

## Track 3 — Operational hardening

Tools the operator needs that don't exist yet.

### 3.1 `garmin-mcp-rotate-data-key` CLI

**Why.** Closes **R5** (chapter 11). Right now the Fernet key
(`GARMIN_MCP_DATA_KEY`) cannot be rotated without manually
decrypting + re-encrypting every row. If the key is suspected to be
compromised the operator's only option is "reset all users".

**What.** New `src/garmin_mcp/maintenance/rotate_data_key.py` with
a `main()` entry point exposed as a script:

```toml
[project.scripts]
garmin-mcp-rotate-data-key = "garmin_mcp.maintenance.rotate_data_key:main"
```

Workflow:

```bash
# Generate a new key
NEW_KEY=$(python -c "from cryptography.fernet import Fernet; \
  print(Fernet.generate_key().decode())")

# Inside the running container — re-encrypts every garmin_tokens row
docker compose exec garmin-mcp \
  garmin-mcp-rotate-data-key --old-key "$OLD" --new-key "$NEW_KEY"

# Update env file with NEW_KEY, restart
```

Algorithm: open SQLite, read every `(user_id, encrypted_blob)`, decrypt
with old Fernet key, re-encrypt with new, UPDATE the row. Atomic per
row (single transaction); if any row fails to decrypt, abort with a
clear error and roll back (no half-rotated state).

**Files.**
- `src/garmin_mcp/maintenance/rotate_data_key.py` (new)
- `pyproject.toml` — register the script
- `infra/azure/scripts/` is for Azure; this script is local — keeps
  in `src/`
- `tests/unit/test_maintenance_rotate.py` — happy path, key
  mismatch, partial failure rollback
- `deploy/README.md` — document the rotation flow under "Day-2 ops"
- `doc/09-architecture-decisions.md` — update ADR-006 (mention the
  rotation tool now exists)
- `doc/11-risks-and-technical-debt.md` — remove R5

**Tests.** Roundtrip on a temp DB with 5 users; rotate, verify all
loadable; corrupt one row before rotation, verify abort + rollback.

**Size.** Medium — ~150 lines.

### 3.2 Off-site backup (restic to S3 / B2)

**Why.** Closes **R1** (chapter 11) — partially. We have local
backups via `deploy/backup.sh`; nothing off-site. A disk failure on
the VPS = all-user re-onboarding.

**What.** New `deploy/backup-offsite.sh` wrapping `restic`:

```bash
#!/usr/bin/env bash
# Snapshot SQLite, then ship to S3-compatible storage via restic.
set -euo pipefail

# Required env (from /etc/garmin-mcp/backup.env, mode 0600):
#   RESTIC_REPOSITORY  s3:s3.eu-central-1.amazonaws.com/my-bucket/garmin-mcp
#   RESTIC_PASSWORD
#   AWS_ACCESS_KEY_ID
#   AWS_SECRET_ACCESS_KEY

source /etc/garmin-mcp/backup.env
TS="$(date -u +%Y%m%dT%H%M%SZ)"

# Reuse the existing local snapshot logic
"${0%/*}/backup.sh" /tmp/garmin-mcp-backup

restic backup /tmp/garmin-mcp-backup --tag "auto-${TS}"
restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
restic check --read-data-subset=10%   # weekly data integrity check

rm -rf /tmp/garmin-mcp-backup
```

Document a recovery procedure: `restic restore latest --target /restore`
on a fresh VPS, `docker compose cp` the SQLite back into the
volume, restart.

**Files.**
- `deploy/backup-offsite.sh` (new)
- `deploy/backup-env.example` (new — template for backup.env)
- `deploy/README.md` — new "Off-site backup" subsection with the
  init + cron examples
- `doc/07-deployment-view.md` — add the off-site arrow on the
  topology diagram
- `doc/11-risks-and-technical-debt.md` — update R1 (mark partial
  mitigation; the SPOF risk itself isn't removed by backups)

**Tests.** Manual on a real VPS — there's no good way to unit-test
this. The script's `bash -n` and `shellcheck` (from track 1.4) catch
syntax issues.

**Size.** Small — ~80 lines + docs.

### 3.3 Caddy `/register` rate limit (defense in depth)

**Why.** Item we deferred from the plan's section 7a. The
application-level guard is in place, but a network-layer limit at
Caddy stops bot floods before they reach the app at all.

**What.** Build a custom Caddy with the
[`caddy-ratelimit`](https://github.com/mholt/caddy-ratelimit)
plugin. `deploy/Dockerfile.caddy` (multi-stage):

```dockerfile
FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/mholt/caddy-ratelimit

FROM caddy:2
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
```

`docker-compose.yml`: switch the caddy service from `image: caddy:2`
to `build: { context: ., dockerfile: deploy/Dockerfile.caddy }`.

`Caddyfile`: add the rate_limit directive on `/register` (10/min/IP).

**Files.**
- `deploy/Dockerfile.caddy` (new)
- `deploy/docker-compose.yml`
- `deploy/Caddyfile`
- `doc/08-crosscutting-concepts.md` — update "Rate limiting" with
  the network layer
- `doc/07-deployment-view.md` — note the custom Caddy build

**Tests.** Bring the stack up locally, `for i in {1..20}; do curl
-X POST http://localhost/register; done` — first 10 succeed, rest
return 429.

**Size.** Small — ~40 lines.

---

## Track 4 — Audit & observability

### 4.1 Tamper-evident audit log

**Why.** Closes **R7** (chapter 11). Append-only-by-convention
isn't enough for forensic non-repudiation; a compromised root
account can rewrite past entries.

**What.** Hash-chain the audit log: each line includes a
`prev_hash` field (SHA-256 of the previous line's full JSON). On
restart we read the last line and continue the chain. A separate
verification CLI walks the file and reports any broken link.

**Files.**
- `src/garmin_mcp/auth/audit.py` — extend `record()` to include
  `prev_hash`
- `src/garmin_mcp/maintenance/verify_audit.py` (new) — CLI
  `garmin-mcp-verify-audit /var/log/garmin-mcp/audit-2026-05-03.log`
- `tests/unit/test_auth_audit.py` — add chain tests
- `doc/08-crosscutting-concepts.md` — update "Logging"
- `doc/11-risks-and-technical-debt.md` — remove R7

**Tests.** Roundtrip: write 100 entries, verify chain. Tamper with
one byte, verify CLI reports the break.

**Size.** Medium — ~100 lines.

### 4.2 Anomaly alerting tripwire

**Why.** Mentioned in plan section 7a as "systemd timer scans the
audit log; alerts if registrations/hour exceeds threshold."

**What.** New `src/garmin_mcp/maintenance/audit_alert.py` —
periodic task in the lifespan (next to `cleanup_loop`) that:
- Reads today's audit file
- Counts events per minute by type
- If `register.success` rate exceeds a threshold (e.g. 10/min) over
  a 5-min window, writes a `WARNING`-level log line to stderr.

For this PR, "alerting" = logging. Routing to email / Slack / push
notification is out of scope. Operators can wire that via
`docker logs --follow` + grep.

**Files.**
- `src/garmin_mcp/maintenance/audit_alert.py` (new)
- `src/garmin_mcp/server.py` — install in
  `background_task_factories`
- `tests/unit/test_maintenance_audit_alert.py`
- `doc/08-crosscutting-concepts.md` — note alerting

**Size.** Small — ~80 lines.

---

## Track 5 — Documentation cleanup

### 5.1 Remove this file

**When.** After all five tracks have landed.

### 5.2 Archive the original plan

**Why.** `~/.claude/plans/hi-analyze-the-current-reactive-phoenix.md`
(the user-side rollout plan) describes work now done. It's not in
the repo, but referenced indirectly. Worth confirming the user is
done with it.

### 5.3 Add README badges

**What.** Update `README.md` with badges for the three workflows
(CI, security, infra) once those workflows are in place.

**Files.** `README.md`

**Size.** 5 lines.

---

## Open questions for review

1. **Track 2.1 — middleware vs cache wrapping.** Middleware has a
   cleaner separation but is harder to wire (FastMCP doesn't expose
   tool-dispatch middleware directly; we'd need to patch the
   StreamableHTTPASGIApp). Cache wrapping is uglier but contained.
   Codex preference?

2. **Track 2.2 — onboarding ticket without `on_success`.** The
   current `OnboardingManager.create_session()` accepts `on_success`
   as optional. For the "session expired mid-conversation" case, we
   want the user to re-onboard and *not* be redirected anywhere
   (they're already where they want to be — back in Claude). The
   absence of `on_success` should produce a "you're done, close this
   tab" page. Verify the existing code handles this.

3. **Track 3.1 — rotation should be online or offline?** Online (run
   inside the container while serving traffic) is more convenient but
   risks reading/writing rows the running app is also touching.
   Offline (`docker compose stop`, run rotation, restart) is safer
   but takes the service down. Recommendation: offline; the lock
   contention isn't worth the risk for a quarterly operation.

4. **Track 4.1 — chain across daily file rotation.** The audit log
   rotates daily by filename. Should the hash chain continue across
   days (cross-file) or reset per day? Reset is simpler; cross-file
   is more rigorous. Recommendation: reset per day, document that
   per-day verification is the granularity.

5. **Track 1.6 — workflow consolidation.** Three options listed.
   Defaulting to (b). Confirm.

6. **Order of execution.** Suggested: track 1 first (CI passes
   green), then 2.1 + 2.2 (the two real runtime risks), then
   3.1 (rotation tool — needed before there's enough data to make
   re-onboarding everyone painful), then everything else.

---

## Out of scope (deliberately)

- **High availability / multi-region** — single-VPS SPOF (R1)
  remains accepted; off-site backup brings it from "catastrophic" to
  "rebuildable in an hour", which is the goal.
- **Postgres backend** — same reasoning; SQLite scale ceiling isn't
  hit yet.
- **Slack/email/push alerting from audit anomalies** — track 4.2
  produces log lines, not pages. Routing them is the operator's job.
- **MFA UX redesign** — R3 (chapter 11) accepted as known limitation;
  garth's blocking callback is the constraint.
- **Bicep schema drift CI** — R4 (chapter 11) accepted; manual
  weekly `bicep build` is sufficient.

These deferrals should be re-evaluated only when actual symptoms
appear, not preemptively.
