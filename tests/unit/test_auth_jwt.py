"""Unit tests for the JWT signer/verifier."""

import time

import jwt as pyjwt
import pytest

from garmin_mcp.auth.jwt import JwtSigner, TokenError, generate_signing_key


@pytest.fixture
def signer():
    return JwtSigner(
        signing_key="test-secret-key",
        issuer="https://garmin-mcp.example.com",
        audience="https://garmin-mcp.example.com/mcp",
    )


def test_issue_then_verify_roundtrip(signer):
    issued = signer.issue("user-1", "client-1", ["mcp.use"])
    claims = signer.verify(issued.token)
    assert claims.user_id == "user-1"
    assert claims.client_id == "client-1"
    assert claims.scopes == ["mcp.use"]
    assert claims.expires_at == issued.expires_at


def test_default_ttl_is_one_hour(signer):
    issued = signer.issue("u", "c", [])
    assert 3500 <= issued.expires_at - int(time.time()) <= 3600


def test_custom_ttl_honored(signer):
    issued = signer.issue("u", "c", [], ttl_seconds=60)
    assert 50 <= issued.expires_at - int(time.time()) <= 60


def test_empty_scopes_list_decodes_to_empty(signer):
    issued = signer.issue("u", "c", [])
    assert signer.verify(issued.token).scopes == []


def test_multiple_scopes_serialized_space_separated(signer):
    issued = signer.issue("u", "c", ["mcp.use", "garmin.read"])
    # peek inside without verification
    raw = pyjwt.decode(issued.token, options={"verify_signature": False})
    assert raw["scope"] == "mcp.use garmin.read"
    assert signer.verify(issued.token).scopes == ["mcp.use", "garmin.read"]


def test_verify_rejects_expired_token(signer):
    issued = signer.issue("u", "c", [], ttl_seconds=-1)
    with pytest.raises(TokenError):
        signer.verify(issued.token)


def test_verify_rejects_wrong_audience(signer):
    other = JwtSigner(
        signing_key="test-secret-key",
        issuer="https://garmin-mcp.example.com",
        audience="https://different.example.com/mcp",
    )
    issued = other.issue("u", "c", [])
    with pytest.raises(TokenError):
        signer.verify(issued.token)


def test_verify_rejects_wrong_issuer(signer):
    other = JwtSigner(
        signing_key="test-secret-key",
        issuer="https://attacker.example.com",
        audience="https://garmin-mcp.example.com/mcp",
    )
    issued = other.issue("u", "c", [])
    with pytest.raises(TokenError):
        signer.verify(issued.token)


def test_verify_rejects_wrong_signature(signer):
    other = JwtSigner(
        signing_key="different-secret",
        issuer="https://garmin-mcp.example.com",
        audience="https://garmin-mcp.example.com/mcp",
    )
    issued = other.issue("u", "c", [])
    with pytest.raises(TokenError):
        signer.verify(issued.token)


def test_verify_rejects_garbage(signer):
    with pytest.raises(TokenError):
        signer.verify("not.a.jwt")


def test_empty_signing_key_rejected():
    with pytest.raises(ValueError):
        JwtSigner(signing_key="", issuer="https://x", audience="https://x")


def test_generate_signing_key_returns_unique_strings():
    a = generate_signing_key()
    b = generate_signing_key()
    assert a != b
    assert len(a) >= 32  # at least 32 chars after base64url encoding
