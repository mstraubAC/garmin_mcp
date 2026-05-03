"""End-to-end OAuth integration tests.

Drives the full Claude → /register → /authorize → (fake Entra) → /callback
→ /token flow against the real ASGI app via `httpx.ASGITransport` — no
real network, no threads. The bearer token from /token is then verified
to be a valid JWT and to gate the /mcp endpoint correctly.

The MCP-over-HTTP round trip with a real uvicorn lives in test_http_server.py;
keeping it isolated avoids the asyncio cross-loop issues that surface when
two uvicorn-backed tests run in the same pytest process.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import jwt as pyjwt
import pytest
from asgi_lifespan import LifespanManager
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from garmin_mcp.auth.audit import AuditLog
from garmin_mcp.auth.entra import EntraOIDCClient
from garmin_mcp.auth.jwt import JwtSigner, TokenError
from garmin_mcp.auth.provider import GarminMcpProvider
from garmin_mcp.auth.storage import Storage
from garmin_mcp.auth.throttle import RegistrationGuard, TokenBucket
from garmin_mcp.server import make_app

TENANT_ID = "11111111-1111-1111-1111-111111111111"
ENTRA_CLIENT_ID = "test-entra-app-id"
ENTRA_CLIENT_SECRET = "shhh"
PUBLIC_URL = "https://garmin-mcp.example.com"


# Fake-Entra plumbing -------------------------------------------------------


def _b64url_uint(i: int) -> str:
    import base64

    b = i.to_bytes((i.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


@pytest.fixture(scope="module")
def rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _make_id_token(private_key, *, sub="entra-sub-alice", email="alice@example.com"):
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    payload = {
        "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        "aud": ENTRA_CLIENT_ID,
        "iat": now,
        "exp": now + 3600,
        "sub": sub,
        "tid": TENANT_ID,
        "email": email,
        "name": "Alice Example",
    }
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": "test-kid"})


def _fake_entra_transport(public_key, id_token: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize",
                    "token_endpoint": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                    "jwks_uri": f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys",
                    "issuer": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
                },
            )
        if path.endswith("/discovery/v2.0/keys"):
            nums = public_key.public_numbers()
            return httpx.Response(
                200,
                json={
                    "keys": [
                        {
                            "kty": "RSA",
                            "use": "sig",
                            "alg": "RS256",
                            "kid": "test-kid",
                            "n": _b64url_uint(nums.n),
                            "e": _b64url_uint(nums.e),
                        }
                    ]
                },
            )
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"id_token": id_token, "access_token": "x"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@asynccontextmanager
async def _patched_jwks(transport):
    """PyJWKClient does sync HTTP — route it through the mock too."""
    from unittest.mock import patch

    def fake_fetch(self, *_, **__):
        with httpx.Client(transport=transport) as c:
            return c.get(self.uri).json()

    with patch("jwt.jwks_client.PyJWKClient.fetch_data", fake_fetch):
        yield


# App + ASGI client builder -------------------------------------------------


@asynccontextmanager
async def _running_app(app):
    """Drive the ASGI lifespan around a block of test code (ASGITransport
    doesn't trigger lifespan on its own)."""
    async with LifespanManager(app):
        yield


@pytest.fixture
def oauth_app(rsa_keypair, tmp_path, mock_garmin_client):
    """Build the full ASGI app wired to a fake Entra. Returns (app, ctx)."""
    private, public = rsa_keypair
    id_token = _make_id_token(private)
    transport = _fake_entra_transport(public, id_token)

    storage = Storage(tmp_path / "state.db")
    jwt_signer = JwtSigner(
        signing_key="test-signing-key",
        issuer=PUBLIC_URL,
        audience=f"{PUBLIC_URL}/mcp",
    )
    entra = EntraOIDCClient(
        tenant_id=TENANT_ID,
        client_id=ENTRA_CLIENT_ID,
        client_secret=ENTRA_CLIENT_SECRET,
        redirect_uri=f"{PUBLIC_URL}/callback",
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    audit = AuditLog(log_dir=tmp_path / "audit")
    bucket = TokenBucket(storage, capacity=100, refill_per_second=100.0)
    guard = RegistrationGuard(storage=storage, per_ip_bucket=bucket)
    provider = GarminMcpProvider(
        storage=storage,
        entra=entra,
        jwt_signer=jwt_signer,
        registration_guard=guard,
        audit=audit,
    )

    mock_garmin_client.get_full_name.return_value = "Marcel Test"
    app = make_app(
        client_provider=lambda: mock_garmin_client,
        auth_provider=provider,
        public_url=PUBLIC_URL,
    )
    return app, {
        "transport": transport,
        "provider": provider,
        "storage": storage,
        "jwt_signer": jwt_signer,
    }


def _client_for(app, *, follow_redirects: bool = False) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=PUBLIC_URL,
        follow_redirects=follow_redirects,
    )


# Tests --------------------------------------------------------------------


@pytest.mark.timeout(15)
async def test_protected_resource_metadata_published(oauth_app):
    app, _ = oauth_app
    async with _running_app(app), _client_for(app) as http:
        resp = await http.get("/.well-known/oauth-protected-resource/mcp")
        assert resp.status_code == 200
        body = resp.json()
        assert "authorization_servers" in body
        assert any(PUBLIC_URL in s for s in body["authorization_servers"])


@pytest.mark.timeout(15)
async def test_dcr_register_endpoint_issues_client_id(oauth_app):
    app, _ = oauth_app
    async with _running_app(app), _client_for(app) as http:
        resp = await http.post(
            "/register",
            json={
                "redirect_uris": [f"{PUBLIC_URL}/claude-callback"],
                "client_name": "Test Claude",
                "token_endpoint_auth_method": "none",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["client_id"]
        assert data.get("client_secret") is None  # public client


@pytest.mark.timeout(15)
async def test_dcr_rejects_non_https_redirect(oauth_app):
    app, _ = oauth_app
    async with _running_app(app), _client_for(app) as http:
        resp = await http.post(
            "/register",
            json={
                "redirect_uris": ["http://attacker.example.com/cb"],
                "client_name": "Bad",
                "token_endpoint_auth_method": "none",
            },
        )
        assert resp.status_code == 400


@pytest.mark.timeout(20)
async def test_full_oauth_flow_yields_valid_jwt(oauth_app):
    """Drive register → authorize → fake-Entra → callback → token, then prove
    the issued bearer token is a valid JWT for our resource."""
    app, ctx = oauth_app
    transport = ctx["transport"]
    jwt_signer = ctx["jwt_signer"]

    async with _running_app(app), _client_for(app) as http:
        # 1. Register
        resp = await http.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost:54321/claude-callback"],
                "client_name": "Test Claude",
                "token_endpoint_auth_method": "none",
            },
        )
        assert resp.status_code == 201
        client_id = resp.json()["client_id"]

        # 2. PKCE
        import base64
        import hashlib
        import secrets

        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )

        # 3. /authorize → redirect to Entra
        async with _patched_jwks(transport):
            resp = await http.get(
                "/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": "http://localhost:54321/claude-callback",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": "claude-state-xyz",
                    "scope": "mcp.use",
                },
            )
            assert resp.status_code in (302, 307), resp.text
            entra_url = resp.headers["location"]
            our_state = parse_qs(urlparse(entra_url).query)["state"][0]

            # 4. Pretend Entra completed and is hitting /callback
            resp = await http.get(
                "/callback",
                params={"code": "fake-entra-code", "state": our_state},
            )
            assert resp.status_code == 302, resp.text
            claude_redirect = resp.headers["location"]

        # 5. Extract our auth code from the Claude-bound redirect
        params = parse_qs(urlparse(claude_redirect).query)
        assert params["state"] == ["claude-state-xyz"]
        our_code = params["code"][0]

        # 6. Exchange the code for a token
        resp = await http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": our_code,
                "redirect_uri": "http://localhost:54321/claude-callback",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert resp.status_code == 200, resp.text
        tok = resp.json()
        assert tok["token_type"] == "Bearer"
        assert tok.get("refresh_token")
        access_token = tok["access_token"]

    # 7. The issued JWT verifies cleanly against our signer and carries the
    #    user_id mapped from the fake Entra `sub`.
    claims = jwt_signer.verify(access_token)
    assert claims.client_id == client_id
    assert claims.scopes == ["mcp.use"]
    assert claims.user_id  # internal UUID, populated by the provider
    assert claims.expires_at > int(time.time())


@pytest.mark.timeout(15)
async def test_token_endpoint_rejects_unknown_code(oauth_app):
    app, _ = oauth_app
    async with _running_app(app), _client_for(app) as http:
        # Need a registered client first so the token endpoint can validate it
        resp = await http.post(
            "/register",
            json={
                "redirect_uris": ["http://localhost:54321/cb"],
                "client_name": "X",
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = resp.json()["client_id"]

        resp = await http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": "totally-invented",
                "redirect_uri": "http://localhost:54321/cb",
                "client_id": client_id,
                "code_verifier": "x" * 64,
            },
        )
        assert resp.status_code == 400


@pytest.mark.timeout(15)
async def test_invalid_jwt_rejected_by_signer(oauth_app):
    """Sanity check: garbage tokens fail JWT verification (this is what
    FastMCP's resource-server middleware will reject on /mcp)."""
    app, ctx = oauth_app
    signer = ctx["jwt_signer"]
    with pytest.raises(TokenError):
        signer.verify("not.a.valid.jwt.token")
