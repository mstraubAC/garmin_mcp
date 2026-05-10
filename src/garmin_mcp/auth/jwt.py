"""Sign and verify the short-lived access tokens this proxy issues.

These are NOT the Entra tokens — they're our own JWTs, scoped to the MCP
resource, signed with a server-side HS256 key (`JWT_SIGNING_KEY` env var).

Claims:
    sub      internal user_id
    iss      our public URL (e.g. https://garmin-mcp.example.com)
    aud      the MCP resource URL
    cid      the OAuth client (Claude app instance) the token was issued to
    scope    space-separated list of granted scopes
    iat      issued-at (unix seconds)
    exp      expiry (unix seconds)

Key rotation:
    JWT_SIGNING_KEY can be a plain string (single key, no kid) or a JSON
    array of {"kid": "<id>", "key": "<secret>"} objects. The first entry
    is used for signing; all entries are accepted for verification by kid.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from dataclasses import dataclass

import jwt as pyjwt

DEFAULT_ALGORITHM = "HS256"


@dataclass
class IssuedToken:
    token: str
    expires_at: int


@dataclass
class TokenClaims:
    user_id: str
    client_id: str
    scopes: list[str]
    expires_at: int


class TokenError(Exception):
    """Raised when verification fails (expired, bad signature, missing claim)."""


def _parse_signing_keys(raw: str) -> list[tuple[str, str]]:
    """Parse JWT_SIGNING_KEY: plain string → [("default", raw)], JSON array → list of (kid, key)."""
    raw = raw.strip()
    if raw.startswith("["):
        pairs = json.loads(raw)
        if not isinstance(pairs, list) or not pairs:
            raise ValueError("JWT_SIGNING_KEY JSON array must be non-empty")
        return [(p["kid"], p["key"]) for p in pairs]
    return [("default", raw)]


class JwtSigner:
    """HS256 signer/verifier with kid-based key rotation.

    The first (kid, key) pair is used for signing; all pairs are accepted
    for verification by matching the kid header in the token.
    """

    def __init__(
        self,
        signing_key: str,
        issuer: str,
        audience: str,
        algorithm: str = DEFAULT_ALGORITHM,
    ):
        if not signing_key:
            raise ValueError("signing_key is required")
        self._keys: list[tuple[str, str]] = _parse_signing_keys(signing_key)
        self._key_map: dict[str, str] = {kid: key for kid, key in self._keys}
        self._issuer = issuer
        self._audience = audience
        self._algorithm = algorithm

    def issue(
        self,
        user_id: str,
        client_id: str,
        scopes: list[str],
        ttl_seconds: int = 3600,
    ) -> IssuedToken:
        kid, key = self._keys[0]
        now = int(time.time())
        exp = now + ttl_seconds
        payload = {
            "sub": user_id,
            "iss": self._issuer,
            "aud": self._audience,
            "cid": client_id,
            "scope": " ".join(scopes),
            "iat": now,
            "exp": exp,
            "jti": secrets.token_urlsafe(16),
        }
        headers = {"kid": kid}
        token = pyjwt.encode(payload, key, algorithm=self._algorithm, headers=headers)
        return IssuedToken(token=token, expires_at=exp)

    def verify(self, token: str) -> TokenClaims:
        try:
            unverified_headers = pyjwt.get_unverified_header(token)
        except pyjwt.PyJWTError as e:
            raise TokenError(str(e)) from e

        kid = unverified_headers.get("kid", "default")
        key = self._key_map.get(kid)
        if key is None:
            raise TokenError(f"unknown signing key: kid={kid}")

        try:
            payload = pyjwt.decode(
                token,
                key,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["sub", "cid", "iat", "exp", "aud", "iss"]},
            )
        except pyjwt.PyJWTError as e:
            raise TokenError(str(e)) from e

        scope_str = payload.get("scope", "")
        scopes = scope_str.split() if scope_str else []

        return TokenClaims(
            user_id=payload["sub"],
            client_id=payload["cid"],
            scopes=scopes,
            expires_at=payload["exp"],
        )


def generate_signing_key() -> str:
    """Convenience for boot scripts: 32 random bytes, base64url-encoded."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
