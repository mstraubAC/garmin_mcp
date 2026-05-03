"""Unit tests for the Entra OIDC client.

Spins up an httpx mock-transport that pretends to be a tenant's discovery,
JWKS, and token endpoints. Validates the full happy path plus the error
branches we actually rely on.
"""
import json
import time
from unittest.mock import patch

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from garmin_mcp.auth.entra import EntraError, EntraOIDCClient


TENANT_ID = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "app-id-here"
CLIENT_SECRET = "shhh"
REDIRECT_URI = "https://garmin-mcp.example.com/callback"


@pytest.fixture(scope="module")
def rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _jwk(public_key, kid: str = "test-kid") -> dict:
    """Return a JWK dict for an RSA public key."""
    nums = public_key.public_numbers()

    def b64(i: int) -> str:
        import base64
        b = i.to_bytes((i.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")

    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": b64(nums.n),
        "e": b64(nums.e),
    }


def _make_id_token(private_key, *, kid: str = "test-kid", **claims) -> str:
    """Sign an Entra-shaped id_token with the given private key."""
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    payload = {
        "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        "aud": CLIENT_ID,
        "iat": now,
        "exp": now + 3600,
        "sub": "user-sub-123",
        "tid": TENANT_ID,
        **claims,
    }
    return pyjwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


class _MockTransport(httpx.MockTransport):
    def __init__(self, public_key, *, token_response: dict | None = None,
                 token_status: int = 200):
        self.public_key = public_key
        self._token_response = token_response
        self._token_status = token_status
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json={
                "authorization_endpoint": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize",
                "token_endpoint": f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
                "jwks_uri": f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys",
                "issuer": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
            })
        if path.endswith("/discovery/v2.0/keys"):
            return httpx.Response(200, json={"keys": [_jwk(self.public_key)]})
        if path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(self._token_status, json=self._token_response or {})
        return httpx.Response(404)


def _client_with_transport(transport):
    return EntraOIDCClient(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )


def _patch_jwks_for_test(mock_transport):
    """PyJWKClient does sync HTTP. Patch its internal http call to use the
    mock transport instead of going to network."""
    from urllib.request import Request
    def fake_fetch(self, *_, **__):
        # Build a synchronous httpx call against the mock
        url = self.uri
        with httpx.Client(transport=mock_transport) as c:
            return c.get(url).json()
    return patch("jwt.jwks_client.PyJWKClient.fetch_data", fake_fetch)


# Tests ----------------------------------------------------------------------


def test_constructor_rejects_missing_args():
    with pytest.raises(ValueError):
        EntraOIDCClient(tenant_id="", client_id="x", client_secret="x", redirect_uri="x")


@pytest.mark.asyncio
async def test_authorization_url_includes_state_and_redirect(rsa_keypair):
    _, public = rsa_keypair
    transport = _MockTransport(public)
    client = _client_with_transport(transport)
    url = await client.authorization_url(state="abc123")
    assert "client_id=app-id-here" in url
    assert "state=abc123" in url
    assert "redirect_uri=https%3A%2F%2Fgarmin-mcp.example.com%2Fcallback" in url
    assert "scope=openid+profile+email" in url


@pytest.mark.asyncio
async def test_exchange_code_happy_path(rsa_keypair):
    private, public = rsa_keypair
    id_token = _make_id_token(
        private,
        email="alice@example.com",
        name="Alice Example",
        preferred_username="alice@example.com",
    )
    transport = _MockTransport(public, token_response={"id_token": id_token})
    client = _client_with_transport(transport)
    with _patch_jwks_for_test(transport):
        identity = await client.exchange_code("code-from-entra")
    assert identity.sub == "user-sub-123"
    assert identity.tid == TENANT_ID
    assert identity.email == "alice@example.com"
    assert identity.name == "Alice Example"


@pytest.mark.asyncio
async def test_exchange_code_token_endpoint_error(rsa_keypair):
    _, public = rsa_keypair
    transport = _MockTransport(public, token_response={"error": "invalid_grant"},
                               token_status=400)
    client = _client_with_transport(transport)
    with pytest.raises(EntraError, match="token endpoint"):
        await client.exchange_code("bad-code")


@pytest.mark.asyncio
async def test_exchange_code_missing_id_token(rsa_keypair):
    _, public = rsa_keypair
    transport = _MockTransport(public, token_response={"access_token": "x"})
    client = _client_with_transport(transport)
    with pytest.raises(EntraError, match="no id_token"):
        await client.exchange_code("code")


@pytest.mark.asyncio
async def test_exchange_code_wrong_audience(rsa_keypair):
    private, public = rsa_keypair
    id_token = _make_id_token(private, aud="some-other-app")
    transport = _MockTransport(public, token_response={"id_token": id_token})
    client = _client_with_transport(transport)
    with _patch_jwks_for_test(transport):
        with pytest.raises(EntraError, match="validation failed"):
            await client.exchange_code("code")


@pytest.mark.asyncio
async def test_exchange_code_wrong_tenant_id(rsa_keypair):
    private, public = rsa_keypair
    id_token = _make_id_token(private, tid="wrong-tenant-id-aaaaaaaaaaaaaaaaaa")
    transport = _MockTransport(public, token_response={"id_token": id_token})
    client = _client_with_transport(transport)
    with _patch_jwks_for_test(transport):
        with pytest.raises(EntraError, match="tid"):
            await client.exchange_code("code")
