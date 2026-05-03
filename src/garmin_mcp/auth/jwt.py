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
"""

from __future__ import annotations

import base64
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


class JwtSigner:
    """HS256 signer/verifier. One instance per process; thread-safe."""

    def __init__(
        self,
        signing_key: str,
        issuer: str,
        audience: str,
        algorithm: str = DEFAULT_ALGORITHM,
    ):
        if not signing_key:
            raise ValueError("signing_key is required")
        self._key = signing_key
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
            # `jti` lets us implement single-use semantics later if needed.
            "jti": secrets.token_urlsafe(16),
        }
        token = pyjwt.encode(payload, self._key, algorithm=self._algorithm)
        return IssuedToken(token=token, expires_at=exp)

    def verify(self, token: str) -> TokenClaims:
        try:
            payload = pyjwt.decode(
                token,
                self._key,
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
