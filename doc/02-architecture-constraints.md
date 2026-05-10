# 2. Architecture constraints

Things that aren't open to redesign. Each constraint is followed by the
direct architectural consequence it forces.

## Technical constraints

| # | Constraint | Consequence |
|---|---|---|
| C1 | **MCP protocol, streamable-HTTP transport** (spec rev 2025-06-18) | Server must speak HTTP+SSE on `/mcp`, expose RFC 9728 protected-resource metadata, and require Bearer JWT |
| C2 | **Claude apps require OAuth 2.1 with Dynamic Client Registration (RFC 7591)** | Server has to expose a `/register` endpoint and persist a per-client record |
| C3 | **Microsoft Entra ID does NOT support DCR** | The server can't redirect Claude directly at Entra; we must run an OAuth proxy that speaks DCR to Claude and pre-registered-client-OAuth to Entra |
| C4 | **Garmin Connect has no public OAuth** for third parties | We collect each user's Garmin email + password during onboarding, log in via [`garth`](https://github.com/matin/garth), persist the resulting OAuth tokens, and discard the password |
| C5 | **Garmin's `garth.login()` blocks on a synchronous `prompt_mfa` callback** | Onboarding can't be a single request — we run garth in a worker thread and bridge to the async web layer with a `threading.Event` + `Queue` |
| C6 | **Python 3.10+** (inherited from the upstream package) | Standard library asyncio + threading; no library that requires 3.13-only features |
| C7 | **Garmin Connect rate-limits per IP** | All authenticated users share the VPS's outbound IP; per-user app-side throttling is needed to keep one user from exhausting the budget |

## Organizational constraints

| # | Constraint | Consequence |
|---|---|---|
| O1 | **One person operates this** | No on-call, no Kubernetes, no managed Postgres; everything has to be deployable + debuggable from a laptop SSH'd into a VPS |
| O2 | **Single Microsoft 365 tenant** | App registration is single-tenant (`AzureADMyOrg`); no multi-tenant complexity |
| O3 | **No paid SaaS dependency** beyond what's already paid for | No Auth0/Clerk/Stytch; we run the OAuth proxy ourselves |
| O4 | **Personal health data** — correctness and privacy are regulatory and ethical obligations | This system handles Garmin Connect data (fitness, sleep, heart rate, weight, nutrition). Even though it is not a regulated medical device, it must be built with the same diligence: no silent data corruption, no cross-user leaks, no unencrypted storage, and every data path must be tested. |

## Conventions

| # | Convention | Why |
|---|---|---|
| K1 | **Tools stay sync-or-async-Python and never see auth** | Tools are the part most likely to change; isolating them from the auth layer keeps churn local |
| K2 | **Tests live alongside the code they cover** | `tests/unit/test_auth_*` mirrors `src/garmin_mcp/auth/*`; one test file per module |
| K3 | **One module per OAuth concern** under `src/garmin_mcp/auth/` | `storage.py`, `jwt.py`, `entra.py`, `provider.py`, `onboarding.py`, … — each one owns one job |
| K4 | **Schema migrations are forward-compat-only** | New tables added with `CREATE TABLE IF NOT EXISTS`; never drop or alter existing columns |
| K5 | **Background work runs in-process** | Cleanup + onboarding workers live inside the uvicorn process; no separate cron, no second container |
