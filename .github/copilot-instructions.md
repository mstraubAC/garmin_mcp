# GitHub Copilot — repository instructions

GitHub Copilot reads this file automatically for every suggestion in
this repo. The full set of conventions for any AI assistant lives in
[`AGENTS.md`](AGENTS.md); this file mirrors the parts most relevant
to inline code suggestions.

## Definition of Done

A code change is **not done** until the matching docs and tests are
updated in the same change set:

1. Code change → at least one test for it (new test or updated existing).
2. Anything described by an arc42 chapter changed → update the relevant
   chapter under [`doc/`](doc/):
   - building blocks → `doc/05-building-block-view.md`
   - runtime flows → `doc/06-runtime-view.md`
   - deployment / env vars / mounts → `doc/07-deployment-view.md`
   - load-bearing decision → new ADR in `doc/09-architecture-decisions.md`
   - new fragility → `doc/11-risks-and-technical-debt.md`
3. The contributor-facing surface changed (operating modes,
   prerequisites, project layout, Quick Start) → update
   [`README.md`](README.md).

## Code conventions

- Tools call `get_garmin_client()` — never pass `user_id` into a tool.
- Schema migrations: `CREATE TABLE IF NOT EXISTS` only; never alter or
  drop existing columns.
- One concern per module under `src/garmin_mcp/auth/`.
- Secrets come from env vars; never hardcode defaults for required
  ones.
- Background work runs in-process via
  `make_app(background_task_factories=[...])`.

## Architectural guardrails (from arc42 docs under `doc/`)

The arc42 architecture documentation is the source of truth for this
repository. Follow the decisions recorded in `doc/09-architecture-decisions.md`
and the requirements in `doc/08-crosscutting-concepts.md` until those
documents are revised by a new ADR.

If a proposed code change would violate an existing arc42 decision,
it must be handled explicitly before implementation:

- Option A: create a new ADR in `doc/09-architecture-decisions.md`
  that clearly explains the rationale and trade-offs.
- Option B: if the change is accepted as technical debt, record it in
  the appropriate technical debt log under `doc/11-risks-and-technical-debt.md`.

Under any circumstance, ask the user which path to take: ADR or
technical debt entry.

**Tests and quality gates.**
- Pre-commit: ruff (lint + format), bandit, trailing-whitespace,
  YAML/JSON/TOML checks.
- Pre-push: full test suite + coverage ≥78%.
- CI: ruff check, ruff format, mypy, pytest, coverage, pip-audit.

**Medical application diligence.**
- This repo handles personal health data and behaves like a medical
  application. The agent must be extra thorough: prioritize correctness,
  security, and data privacy over speed or conciseness.
- Any change that affects data handling, accuracy, or security must be
  validated with explicit test coverage and documented in the appropriate
  `doc/` chapters.


## Test conventions

- Tests live alongside the code they cover —
  `tests/unit/test_auth_<module>.py` mirrors `src/garmin_mcp/auth/<module>.py`.
- Integration tests use `httpx.ASGITransport` + `asgi_lifespan.LifespanManager`
  to drive the app without uvicorn (avoids asyncio cross-loop issues
  between tests).
- `uv run pytest` is the runner; don't suggest alternatives.
