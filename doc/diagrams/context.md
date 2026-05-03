# System context (C4 level 1)

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
