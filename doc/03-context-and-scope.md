# 3. Context and scope

## System context

```mermaid
flowchart LR
    user(["Authenticated user<br/>(via a Claude app)"])

    subgraph claude_clients["Claude clients (mobile / web / desktop)"]
        claude_mobile["Claude mobile"]
        claude_web["Claude web"]
        claude_desktop["Claude desktop"]
    end

    mcp[["Garmin MCP server<br/>this system"]]

    subgraph external["External systems"]
        entra["Microsoft Entra ID<br/>(your O365 tenant)"]
        garmin["Garmin Connect API"]
        le["Let's Encrypt"]
    end

    user -->|opens| claude_mobile
    user -->|opens| claude_web
    user -->|opens| claude_desktop

    claude_mobile -->|MCP over HTTPS<br/>+ Bearer JWT| mcp
    claude_web -->|MCP over HTTPS<br/>+ Bearer JWT| mcp
    claude_desktop -->|MCP over HTTPS<br/>+ Bearer JWT| mcp

    mcp -->|OIDC sign-in| entra
    mcp -->|fetch user data,<br/>upload workouts| garmin
    mcp -->|TLS cert via<br/>HTTP-01 (Caddy)| le

    classDef system fill:#0a5,stroke:#063,color:white
    classDef external fill:#eee,stroke:#888
    class mcp system
    class entra,garmin,le external
```

## Communications

| Counterpart | Direction | Channel | What flows |
|---|---|---|---|
| **Claude client** | both | HTTPS (streamable HTTP, SSE for streaming) | MCP JSON-RPC requests + responses; Bearer JWT in `Authorization` |
| **Claude client (browser)** | inbound | HTTPS | OAuth flow: `/register`, `/authorize`, `/token`, `/onboard*` |
| **Microsoft Entra ID** | outbound | HTTPS | OIDC discovery, auth-code exchange, JWKS fetch |
| **Microsoft Entra ID** | inbound | HTTPS (browser redirect) | `/callback?code=…` after the user signs in |
| **Garmin Connect** | outbound | HTTPS (via [`garth`](https://github.com/matin/garth)) | Login during onboarding; tool-driven API calls per request |
| **Let's Encrypt** | outbound (via Caddy) | HTTP-01 + TLS | Cert issuance + auto-renewal |

## In scope

- Multi-user OAuth-protected MCP server reachable on a public hostname
- One-time per-user onboarding (with MFA support) for Garmin Connect
- Encryption-at-rest for Garmin OAuth tokens
- Per-user isolation for tool execution
- Single-VPS deployment via Docker Compose
- DCR-driven dynamic registration of Claude client instances
- Self-pruning state (TTL on unused clients, expired pending auths)

## Out of scope

- High availability / multi-region (single VPS)
- Garmin password recovery — if a user changes their Garmin password, they
  re-onboard
- Anything beyond the MCP tool surface inherited from upstream (no UI for
  viewing data, no notifications, no scheduled reports)
- Multi-tenant Entra ID — single-tenant only
- Service-account flows for Garmin (Garmin doesn't expose those)
- Anything outside the protected resource — analytics, billing, admin UI

## Boundaries

- **The MCP server never holds a user's Garmin password longer than one
  HTTP request.** Onboarding takes the password, immediately calls
  `garth.login()`, persists only the resulting OAuth tokens (encrypted),
  and zeros the variable.
- **Entra ID is the source of truth for "who is this user".** A local
  user_id is allocated on first sign-in (UUID), but it always maps back
  to the `(entra_sub, entra_tid)` pair recorded at creation.
- **The MCP tool surface is *unchanged* from upstream.** This rollout
  added the network and identity layers; tool implementations were only
  touched to swap one global lookup for a function call (see ADR-005).
