# 10. Quality requirements

## Quality tree

```
Garmin MCP — quality
├── Security
│   ├── Per-user data isolation
│   ├── Encryption at rest (Garmin tokens)
│   ├── Defenses against DCR flooding
│   └── Short-lived signed access tokens
├── Usability
│   ├── Onboarding finishes in < 2 minutes (incl. MFA)
│   ├── No manual API-key fiddling — works in the standard Claude OAuth flow
│   └── Clear errors that point the user at the right action
├── Operability
│   ├── One-command deploy (docker compose up)
│   ├── One-command backup
│   └── Recovery from VPS loss in < 1 hour
└── Maintainability
    ├── Adding a tool touches one file (no auth coupling)
    ├── Each auth concern is one module
    └── Schema upgrades are forward-compat-only
```

## Scenarios

Concrete, testable. Each one names the quality goal it serves.

### S1 — Per-user data isolation (Security)

> Two Entra users (Alice and Bob) are simultaneously authenticated. Alice
> calls `get_activities`. The response contains *only* activities from
> Alice's Garmin account. Bob's parallel call to the same tool returns
> *only* his activities.

Verified by: `tests/integration/test_onboarding_flow.py::test_returning_user_skips_onboarding` (proves
the user_id ends up in the JWT) +
`tests/unit/test_user_context.py::test_multi_user_cache_isolates_users`
(proves the cache returns different clients).

### S2 — Onboarding completes within 2 minutes (Usability)

> A new user signs in via Entra, completes the Garmin onboarding form,
> enters the MFA code received within 30 s, and is redirected back to
> Claude with a working access token. End-to-end wall-clock time:
> < 2 minutes.

Verified by manual test on first deploy. Bounded by:
- Entra's interactive sign-in: ~10 s
- Garmin's MFA delivery: 5–30 s
- Three round-trips to MCP: < 1 s combined

### S3 — Recovery from VPS loss (Operability)

> A new VPS is provisioned. Operator clones the repo, copies
> `/etc/garmin-mcp/env` from backup, restores the latest SQLite snapshot
> into the `garmin-mcp-data` volume, runs `docker compose up -d --build`.
> Within 60 minutes (incl. DNS propagation if changed) the system is
> back up and existing users can sign in without re-onboarding.

Verified by: documented in [`deploy/README.md`](../deploy/README.md);
exercise-backed when step 9 lands the restic-based backup script.

### S4 — DCR flooding doesn't degrade the system (Security)

> An attacker POSTs 10,000 `/register` requests from a single IP in 1 hour.
> The server rejects > 5 of them with 429 (per-IP token bucket); the
> oauth_clients table grows by ≤ 5 rows; legitimate users on other IPs
> can still register and sign in.

Verified by: `tests/unit/test_auth_throttle.py::test_check_per_ip_uses_bucket`
+ `test_under_global_cap`.

### S5 — Adding a new tool touches one file (Maintainability)

> A developer adds a `get_steps_30day_average` tool. They edit
> `src/garmin_mcp/health_wellness.py` (one file). They write a unit test
> in `tests/integration/test_health_wellness_tools.py` (one file). They
> do NOT touch any file under `auth/`. Total diff: ~30 lines.

Verified by: design — tools call `get_garmin_client()` and have no
auth-related parameters or imports. Existing tools in `health_wellness.py`
demonstrate this.

### S6 — Schema upgrade with no downtime data loss (Operability)

> Operator pulls a new image that adds a new SQLite table. Restart the
> container. Existing data is untouched; the new table is created
> automatically. No manual migration step.

Verified by: design (`CREATE TABLE IF NOT EXISTS` + version bump). Step
4 → Step 5 was an actual instance of this (added `garmin_tokens` table,
schema v1 → v2).

### S7 — Garmin token corruption fails loudly (Security)

> The `GARMIN_MCP_DATA_KEY` env var is set to a value that doesn't match
> the key used to encrypt existing rows. The next user trying to call a
> tool gets a clear error pointing at "data key was rotated"; the
> server doesn't silently corrupt or hand back invalid data.

Verified by: `tests/unit/test_auth_garmin_tokens.py::test_wrong_key_cannot_decrypt`.
