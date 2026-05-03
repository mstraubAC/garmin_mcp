# 12. Glossary

| Term | Meaning |
|---|---|
| **arc42** | A documentation template for software architecture, structured as 12 chapters. https://arc42.org |
| **Bearer token** | An access credential where possession of the string is enough to authenticate; sent in the `Authorization: Bearer <token>` HTTP header. |
| **Caddy** | A web server / reverse proxy with automatic HTTPS via Let's Encrypt. Used here as the TLS terminator in front of the MCP container. |
| **DCR** (Dynamic Client Registration) | RFC 7591 — an OAuth client posts metadata (redirect URIs, name) to `/register` and gets back a fresh `client_id` (and optionally `client_secret`), no manual app registration needed. |
| **Entra ID** | Microsoft Entra ID, formerly known as Azure Active Directory. The identity provider for our O365 tenant. |
| **Fernet** | A symmetric authenticated encryption scheme from the [cryptography](https://cryptography.io/en/latest/fernet/) library: AES-128-CBC + HMAC-SHA256, with key + IV management baked in. |
| **garth** | The Python library for talking to Garmin Connect, including the OAuth1+OAuth2 dance Garmin requires. https://github.com/matin/garth |
| **htmx** | A small JavaScript library that lets HTML elements trigger server requests and swap response fragments into the DOM. Used for the onboarding MFA polling without a heavier SPA framework. https://htmx.org |
| **JWT** (JSON Web Token) | A compact, URL-safe, signed JSON object used here as the MCP access token. Self-contained — no DB lookup needed to verify. |
| **JWKS** (JSON Web Key Set) | A document containing public keys, fetched at the OIDC `jwks_uri`. We use it to validate Entra's `id_token` signatures. |
| **MCP** (Model Context Protocol) | The Anthropic-led open protocol for connecting AI tools to data sources. https://modelcontextprotocol.io |
| **OIDC** (OpenID Connect) | An identity layer on top of OAuth 2.0; issues an `id_token` describing the authenticated user. We use it to authenticate users via Entra. |
| **PKCE** (Proof Key for Code Exchange) | RFC 7636 — protects an OAuth authorization code against interception by tying the code to a `code_verifier` only the original client knows. |
| **RFC 9728** | OAuth 2.0 Protected Resource Metadata. Defines the `/.well-known/oauth-protected-resource` endpoint that tells clients which authorization servers issue tokens for this resource. |
| **`sub` (Entra)** | The Subject claim in an Entra `id_token` — a stable, opaque identifier for the authenticated user within the tenant. We use `(sub, tid)` as the primary key for our `users` table. |
| **`tid` (Entra)** | The Tenant ID claim — identifies the Entra tenant the user signed into. Single-tenant means we only accept one value. |
| **Streamable-HTTP** | The MCP transport that replaced SSE in the 2025-06-18 spec. Plain HTTP request/response with optional server-sent-events for streaming partial output. |
| **WAL** (Write-Ahead Log) | A SQLite journaling mode where writes go to a separate log file before being merged back; lets readers proceed concurrently with writes. We enable it via `PRAGMA journal_mode = WAL`. |
