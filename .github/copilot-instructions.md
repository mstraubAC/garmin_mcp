# GitHub Copilot — repository instructions

GitHub Copilot reads this file automatically for every suggestion in
this repo. The full set of conventions for any AI assistant lives in
[`AGENTS.md`](../AGENTS.md); this file mirrors the parts most relevant
to inline code suggestions.

## Definition of Done

A code change is **not done** until the matching docs and tests are
updated in the same change set:

1. Code change → at least one test for it (new test or updated existing).
2. Anything described by an arc42 chapter changed → update the relevant
   chapter under [`doc/`](../doc/):
   - building blocks → `doc/05-building-block-view.md`
   - runtime flows → `doc/06-runtime-view.md`
   - deployment / env vars / mounts → `doc/07-deployment-view.md`
   - load-bearing decision → new ADR in `doc/09-architecture-decisions.md`
   - new fragility → `doc/11-risks-and-technical-debt.md`
3. The contributor-facing surface changed (operating modes,
   prerequisites, project layout, Quick Start) → update
   [`README.md`](../README.md).

## Code conventions

- Tools call `get_garmin_client()` — never pass `user_id` into a tool.
- Schema migrations: `CREATE TABLE IF NOT EXISTS` only; never alter or
  drop existing columns.
- One concern per module under `src/garmin_mcp/auth/`.
- Secrets come from env vars; never hardcode defaults for required
  ones.
- Background work runs in-process via
  `make_app(background_task_factories=[...])`.

## Test conventions

- Tests live alongside the code they cover —
  `tests/unit/test_auth_<module>.py` mirrors `src/garmin_mcp/auth/<module>.py`.
- Integration tests use `httpx.ASGITransport` + `asgi_lifespan.LifespanManager`
  to drive the app without uvicorn (avoids asyncio cross-loop issues
  between tests).
- `uv run pytest` is the runner; don't suggest alternatives.
