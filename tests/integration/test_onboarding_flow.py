"""End-to-end onboarding test.

Exercises the new-user path:

  1. Claude registers a client (DCR)
  2. Claude hits /authorize → we redirect to (fake) Entra
  3. Entra → /callback. User has no Garmin tokens, so we redirect to /onboard
  4. User submits Garmin credentials (fake Garmin needs MFA)
  5. User submits the MFA code
  6. Onboarding completes → /onboard/status returns the Claude redirect URL
  7. /token exchange yields an access token whose user_id is the new user

Plus: the *returning user* path skips onboarding entirely.

Uses httpx.ASGITransport (no real network), and a fake Garmin client so we
can script MFA behavior synchronously.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

import httpx
import jwt as pyjwt
import pytest
from asgi_lifespan import LifespanManager
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from garminconnect import GarminConnectAuthenticationError

from garmin_mcp.auth.audit import AuditLog
from garmin_mcp.auth.entra import EntraOIDCClient
from garmin_mcp.auth.garmin_tokens import GarminTokenStore
from garmin_mcp.auth.jwt import JwtSigner
from garmin_mcp.auth.onboarding import OnboardingManager
from garmin_mcp.auth.provider import GarminMcpProvider
from garmin_mcp.auth.storage import Storage
from garmin_mcp.auth.throttle import RegistrationGuard, TokenBucket
from garmin_mcp.server import make_app
from garmin_mcp.user_context import MultiUserClientCache

TENANT_ID = "11111111-1111-1111-1111-111111111111"
ENTRA_CLIENT_ID = "test-entra-app-id"
PUBLIC_URL = "https://garmin-mcp.example.com"


# ---- fake Garmin --------------------------------------------------------


class FakeGarmin:
    """Test double for `garminconnect.Garmin`. Behavior is set on the
    factory's nonlocal so individual tests can flip the script."""

    behavior = "needs_mfa_then_succeeds"
    expected_mfa_code = "123456"

    def __init__(self, *, email, password, is_cn, prompt_mfa):
        self._email = email
        self._password = password
        self._prompt_mfa = prompt_mfa
        self.garth = self

    def login(self):
        if FakeGarmin.behavior == "bad_password":
            raise GarminConnectAuthenticationError("invalid credentials")
        if FakeGarmin.behavior in ("needs_mfa", "needs_mfa_then_succeeds"):
            code = self._prompt_mfa()
            if code != FakeGarmin.expected_mfa_code:
                raise GarminConnectAuthenticationError("MFA code incorrect")

    def dumps(self) -> str:
        return f"garth-blob-{self._email}"

    def loads(self, blob: str) -> None:
        pass

    # Surface a method tools might call so the cache test is realistic
    def get_full_name(self) -> str:
        return "Onboarded User"


# ---- fake Entra ---------------------------------------------------------


def _b64url_uint(i: int) -> str:
    import base64

    b = i.to_bytes((i.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


@pytest.fixture(scope="module")
def rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _make_id_token(private_key, *, sub, email):
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    now = int(time.time())
    return pyjwt.encode(
        {
            "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
            "aud": ENTRA_CLIENT_ID,
            "iat": now,
            "exp": now + 3600,
            "sub": sub,
            "tid": TENANT_ID,
            "email": email,
            "name": email.split("@")[0],
        },
        pem,
        algorithm="RS256",
        headers={"kid": "test-kid"},
    )


def _fake_entra_transport(public_key, id_token):
    def handler(request):
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
            return httpx.Response(200, json={"id_token": id_token})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@asynccontextmanager
async def _patched_jwks(transport):
    from unittest.mock import patch

    def fake_fetch(self, *_, **__):
        with httpx.Client(transport=transport) as c:
            return c.get(self.uri).json()

    with patch("jwt.jwks_client.PyJWKClient.fetch_data", fake_fetch):
        yield


@asynccontextmanager
async def _running(app):
    async with LifespanManager(app):
        yield


def _client(app, **kw):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=PUBLIC_URL,
        **kw,
    )


# ---- shared app builder -------------------------------------------------


@pytest.fixture
def app_with_onboarding(rsa_keypair, tmp_path):
    private, public = rsa_keypair
    id_token = _make_id_token(private, sub="entra-sub-alice", email="alice@example.com")
    transport = _fake_entra_transport(public, id_token)

    storage = Storage(tmp_path / "state.db")
    token_store = GarminTokenStore(storage, Fernet.generate_key().decode())
    audit = AuditLog(log_dir=tmp_path / "audit")
    bucket = TokenBucket(storage, capacity=100, refill_per_second=100.0)

    # Fresh fake Garmin per test to reset behavior
    FakeGarmin.behavior = "needs_mfa_then_succeeds"
    onboarding = OnboardingManager(token_store, garmin_factory=lambda **kw: FakeGarmin(**kw))

    jwt_signer = JwtSigner(signing_key="test-key", issuer=PUBLIC_URL, audience=f"{PUBLIC_URL}/mcp")
    entra = EntraOIDCClient(
        tenant_id=TENANT_ID,
        client_id=ENTRA_CLIENT_ID,
        client_secret="shhh",
        redirect_uri=f"{PUBLIC_URL}/callback",
        http_client_factory=lambda: httpx.AsyncClient(transport=transport),
    )
    provider = GarminMcpProvider(
        storage=storage,
        entra=entra,
        jwt_signer=jwt_signer,
        registration_guard=RegistrationGuard(storage=storage, per_ip_bucket=bucket),
        audit=audit,
        garmin_tokens=token_store,
        onboarding=onboarding,
        public_url=PUBLIC_URL,
    )
    cache = MultiUserClientCache(
        token_store,
        garmin_factory=lambda: FakeGarmin(
            email="x", password="x", is_cn=False, prompt_mfa=lambda: "x"
        ),
    )
    app = make_app(
        client_cache=cache,
        auth_provider=provider,
        public_url=PUBLIC_URL,
        onboarding_manager=onboarding,
    )
    return app, {
        "transport": transport,
        "provider": provider,
        "storage": storage,
        "token_store": token_store,
        "onboarding": onboarding,
        "jwt_signer": jwt_signer,
    }


# ---- helpers to drive the OAuth flow -----------------------------------


async def _drive_to_callback(http, transport):
    """Returns the URL the /callback handler redirects the browser to.
    For a NEW user, this is /onboard?ticket=...; for a returning user it's
    Claude's redirect_uri with our auth code."""
    resp = await http.post(
        "/register",
        json={
            "redirect_uris": ["http://localhost:54321/cb"],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
        },
    )
    assert resp.status_code == 201
    client_id = resp.json()["client_id"]

    import base64
    import hashlib
    import secrets

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )

    async with _patched_jwks(transport):
        resp = await http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "http://localhost:54321/cb",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "claude-state",
                "scope": "mcp.use",
            },
        )
        assert resp.status_code in (302, 307)
        our_state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]

        resp = await http.get(
            "/callback",
            params={"code": "fake-entra-code", "state": our_state},
        )
        assert resp.status_code == 302
    return resp.headers["location"], client_id, verifier


def _wait_for_state(onboarding, ticket, predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = onboarding.get(ticket)
        if s and predicate(s.state):
            return s
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting; last state = {onboarding.get(ticket).state}")


# ---- tests ------------------------------------------------------------


@pytest.mark.timeout(15)
async def test_new_user_is_redirected_to_onboard(app_with_onboarding):
    app, ctx = app_with_onboarding
    async with _running(app), _client(app, follow_redirects=False) as http:
        location, _, _ = await _drive_to_callback(http, ctx["transport"])
        assert location.startswith(f"{PUBLIC_URL}/onboard?ticket=")


@pytest.mark.timeout(15)
async def test_full_onboarding_completes_and_oauth_resumes(app_with_onboarding):
    app, ctx = app_with_onboarding
    async with _running(app), _client(app, follow_redirects=False) as http:
        location, client_id, verifier = await _drive_to_callback(http, ctx["transport"])
        ticket = parse_qs(urlparse(location).query)["ticket"][0]

        # GET /onboard renders the credentials form
        resp = await http.get("/onboard", params={"ticket": ticket})
        assert resp.status_code == 200
        assert "Garmin email" in resp.text

        # Submit credentials → server starts the worker
        resp = await http.post(
            "/onboard/credentials",
            data={"ticket": ticket, "email": "alice@x.com", "password": "secret"},
        )
        assert resp.status_code == 200

        # Worker hits the prompt_mfa callback and waits for code
        _wait_for_state(
            ctx["onboarding"],
            ticket,
            lambda st: st.value == "AWAITING_MFA",
        )

        # Submit the right MFA code
        resp = await http.post(
            "/onboard/mfa",
            data={"ticket": ticket, "code": "123456"},
        )
        assert resp.status_code == 200

        # Worker finishes → status endpoint reports COMPLETE with redirect URL
        session = _wait_for_state(
            ctx["onboarding"],
            ticket,
            lambda st: st.value == "COMPLETE",
        )
        assert ctx["token_store"].has(session.user_id)

        resp = await http.get("/onboard/status", params={"ticket": ticket})
        assert resp.status_code == 200
        # The COMPLETE panel embeds the redirect URL — confirm it points back
        # at Claude with our auth code
        assert "http://localhost:54321/cb?code=" in resp.text

        # Pull the our_code out of the session.redirect_url and exchange it
        claude_redirect = session.redirect_url
        our_code = parse_qs(urlparse(claude_redirect).query)["code"][0]

        resp = await http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": our_code,
                "redirect_uri": "http://localhost:54321/cb",
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )
        assert resp.status_code == 200
        access_token = resp.json()["access_token"]

    claims = ctx["jwt_signer"].verify(access_token)
    assert claims.user_id == session.user_id


@pytest.mark.timeout(15)
async def test_returning_user_skips_onboarding(app_with_onboarding):
    app, ctx = app_with_onboarding
    # Pre-seed: a user with this Entra sub already has Garmin tokens.
    user = ctx["storage"].get_or_create_user(
        user_id_factory=lambda: "u-existing",
        entra_sub="entra-sub-alice",
        entra_tid=TENANT_ID,
        email="alice@example.com",
        display_name="Alice",
    )
    ctx["token_store"].save(user["user_id"], "garth-blob")

    async with _running(app), _client(app, follow_redirects=False) as http:
        location, _, _ = await _drive_to_callback(http, ctx["transport"])
        # Goes straight back to Claude's redirect_uri, not /onboard
        assert location.startswith("http://localhost:54321/cb?")
        assert "/onboard" not in location


@pytest.mark.timeout(15)
async def test_wrong_mfa_code_marks_failed(app_with_onboarding):
    app, ctx = app_with_onboarding
    async with _running(app), _client(app, follow_redirects=False) as http:
        location, _, _ = await _drive_to_callback(http, ctx["transport"])
        ticket = parse_qs(urlparse(location).query)["ticket"][0]

        await http.post(
            "/onboard/credentials",
            data={"ticket": ticket, "email": "alice@x.com", "password": "secret"},
        )
        _wait_for_state(
            ctx["onboarding"],
            ticket,
            lambda st: st.value == "AWAITING_MFA",
        )

        await http.post("/onboard/mfa", data={"ticket": ticket, "code": "wrong"})
        session = _wait_for_state(
            ctx["onboarding"],
            ticket,
            lambda st: st.value == "FAILED",
        )
        assert "mfa" in session.error_message.lower()
        assert not ctx["token_store"].has(session.user_id)


@pytest.mark.timeout(15)
async def test_onboarding_status_returns_404_for_unknown_ticket(app_with_onboarding):
    app, _ = app_with_onboarding
    async with _running(app), _client(app) as http:
        resp = await http.get("/onboard/status", params={"ticket": "nope"})
        assert resp.status_code == 404


@pytest.mark.timeout(15)
async def test_credentials_endpoint_requires_all_fields(app_with_onboarding):
    app, ctx = app_with_onboarding
    async with _running(app), _client(app) as http:
        # Create a session via the OAuth flow
        location, _, _ = await _drive_to_callback(http, ctx["transport"])
        ticket = parse_qs(urlparse(location).query)["ticket"][0]

        resp = await http.post(
            "/onboard/credentials",
            data={"ticket": ticket, "email": "", "password": ""},
        )
        assert resp.status_code == 400
        assert "required" in resp.text.lower()
