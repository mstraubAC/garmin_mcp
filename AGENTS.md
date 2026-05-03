# Instructions for AI agents working on this repository

This file is the cross-tool convention (Claude Code, OpenAI Codex, Cursor,
Aider, …) for how to work in this repo. GitHub Copilot reads the same
rules from [`.github/copilot-instructions.md`](.github/copilot-instructions.md).

## Definition of Done

A change is **not done** until the code, tests, and documentation are
all updated together in the same change set. "I'll update the docs in a
follow-up" is a regression — by the time the follow-up happens the diff
isn't fresh anymore and something gets missed.

When you finish a code change, before reporting it as complete:

1. **Tests are added or updated.** New behavior → new test. Bug fix →
   regression test. Refactor → existing tests still pass.
2. **`doc/` is updated** if the change touches anything described by an
   arc42 chapter:
   - New file under `src/garmin_mcp/` → [`doc/05-building-block-view.md`](doc/05-building-block-view.md)
   - New step in any auth or onboarding flow → [`doc/06-runtime-view.md`](doc/06-runtime-view.md) (sequence diagrams)
   - New env var, mount, port, or container → [`doc/07-deployment-view.md`](doc/07-deployment-view.md)
   - Crosscutting concern (security, encryption, logging, rate limit) →
     [`doc/08-crosscutting-concepts.md`](doc/08-crosscutting-concepts.md)
   - Load-bearing decision (something where flipping the choice would
     touch ≥3 files) → add an ADR to
     [`doc/09-architecture-decisions.md`](doc/09-architecture-decisions.md)
   - Newly-discovered fragility → add to
     [`doc/11-risks-and-technical-debt.md`](doc/11-risks-and-technical-debt.md);
     remove items you've actually shipped fixes for
   - New jargon → [`doc/12-glossary.md`](doc/12-glossary.md)
3. **[`README.md`](README.md) is updated** if the change touches the
   surface a new contributor first encounters — operating modes,
   prerequisites, project layout, or any command in the Quick Start
   sections.

When in doubt, edit one of the docs. Over-documentation is cheaper to
fix than under-documentation.

## Architecture conventions

These come from existing ADRs — don't reinvent them without writing a
new ADR superseding the relevant one.

- **Tools never see auth.** Tools call `get_garmin_client()` with no
  arguments; the per-request user_id ContextVar resolves the right
  client. If you find yourself plumbing `user_id` into a tool, you're
  fighting the design.
- **Schema migrations are forward-compat-only.** Add tables with
  `CREATE TABLE IF NOT EXISTS` and bump `SCHEMA_VERSION`. Never alter or
  drop existing columns without a real migration framework (which we
  don't have yet — see ADR-003).
- **One auth concern per module under `src/garmin_mcp/auth/`.** Don't
  conflate JWT issuance with storage with rate limiting; the existing
  split (`jwt.py`, `storage.py`, `throttle.py`, `provider.py`,
  `entra.py`, `audit.py`, `garmin_tokens.py`, `onboarding.py`) is
  load-bearing for testability.
- **Background work runs in-process** as asyncio tasks via
  `make_app(background_task_factories=[...])`. No separate cron
  containers, no second processes.
- **Secrets come from env vars only.** No config files, no inline
  defaults. Required vars use `os.environ[...]` (fail fast); optional
  vars use `os.environ.get(...)` with a sensible default.

## Workflow expectations

- **Tests run with `uv run pytest`.** Don't introduce alternative
  runners. The integration tests use `httpx.ASGITransport` + manual
  lifespan via `asgi_lifespan.LifespanManager` — keep that pattern when
  adding flow-level tests.
- **Every change in a separate branch with a focused PR.** The rollout
  history (PRs #2 through #8) is the model: each PR delivers one
  cohesive piece, with its own tests and docs in the same diff.
- **PR descriptions follow the PR #2 template:**
  ```
  ## Summary
  (one paragraph — what this PR does)

  ## Why
  (motivation / context — what problem does this solve?)

  ## What changed
  (bullet list of concrete changes — files, new modules, regressions fixed)

  ## Test plan
  - [ ] (checklist of test scenarios)
  ```
  Every PR description must have these four sections. Don't add
  fluff sections or omit any of them.
- **Don't add dependencies casually.** The current dep set is small on
  purpose. If you do add one, mention it in the PR description with a
  one-line "why this and not stdlib".
- **Don't commit `.claude/`, `.idea/`, `__pycache__/`, or local env
  files.** They're already in `.gitignore` / `.dockerignore`; if a new
  tool introduces another, add it.

## When the rules need to change

Edit this file in the same PR where you change the rule. Don't merge
new conventions and the code that follows them in separate PRs — they
need to be reviewable together.
