# Garmin MCP — architecture documentation

[arc42](https://arc42.org/) layout. Each chapter is a separate file so it
can be read in isolation; cross-references link related sections.

## Reading order

| # | Chapter | What it covers |
|---|---|---|
| 1 | [Introduction and goals](01-introduction-and-goals.md) | Why this server exists, who uses it, what "good" looks like |
| 2 | [Architecture constraints](02-architecture-constraints.md) | Things we don't get to choose (MCP spec, Entra DCR limit, …) |
| 3 | [Context and scope](03-context-and-scope.md) | System boundary diagram + in/out of scope list |
| 4 | [Solution strategy](04-solution-strategy.md) | The five or six load-bearing decisions in one page |
| 5 | [Building block view](05-building-block-view.md) | Whitebox of the ASGI app and the auth package |
| 6 | [Runtime view](06-runtime-view.md) | Sequence diagrams for the four flows that matter |
| 7 | [Deployment view](07-deployment-view.md) | Where things run; volumes, network, secrets |
| 8 | [Crosscutting concepts](08-crosscutting-concepts.md) | AuthN/Z, encryption, logging, rate limiting |
| 9 | [Architecture decisions](09-architecture-decisions.md) | ADRs with full context for the load-bearing choices |
| 10 | [Quality requirements](10-quality-requirements.md) | Quality tree + concrete scenarios |
| 11 | [Risks and technical debt](11-risks-and-technical-debt.md) | What we know is fragile |
| 12 | [Glossary](12-glossary.md) | DCR, MCP, PKCE, JWT, Fernet, garth, … |

## Diagrams

Live in [`diagrams/`](diagrams/). All Mermaid — renders natively in
GitHub's Markdown viewer; no external server needed. The chapter files
embed the diagrams inline; the standalone files in `diagrams/` are the
canonical source.

## When to update

- A new ADR is needed whenever you change a "load-bearing decision"
  (something where flipping the choice would touch ≥3 files). Add it
  to chapter 9 and link from chapter 4.
- Building-block view (chapter 5) needs editing whenever a new module
  appears under `src/garmin_mcp/auth/` or the public surface of
  `make_app` / `make_production_app` changes.
- Runtime view (chapter 6) is the first thing to revisit when an auth
  step changes — those four flows are the contract between the OAuth
  proxy and the rest of the system.
- Risks (chapter 11) is a *living* list — add new items as you find
  them, remove items you've actually fixed.
