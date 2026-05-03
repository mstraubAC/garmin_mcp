# Runtime: first-time user authentication + onboarding

```mermaid
sequenceDiagram
    autonumber
    actor U as User (browser)
    participant CL as Claude client
    participant MCP as MCP server
    participant E as Entra ID
    participant G as Garmin Connect

    Note over CL,MCP: Discovery + DCR
    CL->>MCP: GET /.well-known/oauth-protected-resource/mcp
    MCP-->>CL: { authorization_servers: [<MCP_PUBLIC_URL>] }
    CL->>MCP: POST /register (DCR)
    MCP-->>CL: 201 { client_id, client_secret? }

    Note over CL,U: Authorization code flow
    CL->>U: open /authorize?... in browser
    U->>MCP: GET /authorize?...
    MCP->>MCP: persist pending_authorization, build Entra URL
    MCP-->>U: 302 → Entra login

    U->>E: sign in (single-tenant)
    E-->>U: 302 → MCP /callback?code=...&state=...

    U->>MCP: GET /callback?code=...&state=...
    MCP->>E: POST /token (auth code → id_token)
    E-->>MCP: { id_token }
    MCP->>MCP: validate id_token sig + tid + aud<br/>get_or_create_user(entra_sub, tid)

    Note over MCP: User has no Garmin tokens → divert to /onboard
    MCP->>MCP: onboarding.create_session(user_id, on_success=…)
    MCP-->>U: 302 → /onboard?ticket=…

    Note over U,G: One-time Garmin onboarding (with MFA)
    U->>MCP: GET /onboard?ticket=…
    MCP-->>U: HTML credentials form
    U->>MCP: POST /onboard/credentials (email + password)
    MCP->>MCP: spawn worker thread
    MCP-->>U: panel: AUTHENTICATING

    activate G
    Note right of G: worker thread runs<br/>garth.login()
    G->>MCP: prompt_mfa() callback (blocks)
    MCP->>MCP: state = AWAITING_MFA, queue.get(timeout=5min)

    U->>MCP: GET /onboard/status (htmx polling)
    MCP-->>U: panel: AWAITING_MFA + form
    U->>MCP: POST /onboard/mfa (code)
    MCP->>MCP: queue.put(code); worker resumes
    G-->>MCP: login OK, garth.dumps() → token blob
    deactivate G

    MCP->>MCP: token_store.save(user_id, blob) [Fernet-encrypt]
    MCP->>MCP: on_success → _issue_code_for(pending, user_id)
    MCP->>MCP: state = COMPLETE, redirect_url = Claude cb

    U->>MCP: GET /onboard/status
    MCP-->>U: panel: COMPLETE + JS redirect
    U->>CL: Claude callback?code=…&state=…

    Note over CL,MCP: Token exchange
    CL->>MCP: POST /token (code + PKCE verifier)
    MCP->>MCP: consume_authorization_code, mint JWT + refresh
    MCP-->>CL: { access_token (JWT), refresh_token, expires_in }
```
