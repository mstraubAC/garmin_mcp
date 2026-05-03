"""Microsoft Entra ID OIDC client.

Discovers tenant metadata, builds the authorize URL, exchanges auth codes for
ID tokens, and validates ID-token signatures against the tenant's JWKS.

Uses httpx (already pulled in transitively via mcp). No Microsoft SDK
dependency — keeps the deploy small.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt as pyjwt
from jwt import PyJWKClient


@dataclass
class EntraIdentity:
    """Subset of an Entra ID-token's claims that we actually use."""
    sub: str
    tid: str
    email: str | None
    name: str | None
    preferred_username: str | None


class EntraError(Exception):
    pass


class EntraOIDCClient:
    """One instance per Entra app registration. Thread-safe (httpx + PyJWKClient)."""

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        http_client_factory=None,
    ):
        if not all([tenant_id, client_id, client_secret, redirect_uri]):
            raise ValueError("tenant_id, client_id, client_secret, redirect_uri required")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri

        self._authority = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._discovery_url = f"{self._authority}/.well-known/openid-configuration"
        self._http_factory = http_client_factory or (lambda: httpx.AsyncClient(timeout=10))

        self._metadata: dict[str, Any] | None = None
        self._jwks_client: PyJWKClient | None = None

    async def _ensure_metadata(self) -> dict[str, Any]:
        if self._metadata is not None:
            return self._metadata
        async with self._http_factory() as http:
            resp = await http.get(self._discovery_url)
            resp.raise_for_status()
            self._metadata = resp.json()
        # PyJWKClient does sync IO but caches keys after first fetch — fine
        # for a small number of keys and a long-lived process.
        self._jwks_client = PyJWKClient(self._metadata["jwks_uri"])
        return self._metadata

    async def authorization_url(
        self,
        state: str,
        scopes: list[str] | None = None,
        nonce: str | None = None,
    ) -> str:
        meta = await self._ensure_metadata()
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "response_mode": "query",
            "scope": " ".join(scopes or ["openid", "profile", "email"]),
            "state": state,
        }
        if nonce:
            params["nonce"] = nonce
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> EntraIdentity:
        """Exchange the auth code Entra returned via /callback for an ID token,
        validate the signature, and return the user's identity."""
        meta = await self._ensure_metadata()

        async with self._http_factory() as http:
            resp = await http.post(
                meta["token_endpoint"],
                data={
                    "client_id": self.client_id,
                    "client_secret": self._client_secret,
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if resp.status_code != 200:
            raise EntraError(f"token endpoint returned {resp.status_code}: {resp.text}")

        token_payload = resp.json()
        id_token = token_payload.get("id_token")
        if not id_token:
            raise EntraError("token response had no id_token")

        return self._validate_id_token(id_token)

    def _validate_id_token(self, id_token: str) -> EntraIdentity:
        if self._jwks_client is None:
            raise EntraError("OIDC metadata not loaded; call discover() first")
        signing_key = self._jwks_client.get_signing_key_from_jwt(id_token).key
        try:
            claims = pyjwt.decode(
                id_token,
                signing_key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=f"https://login.microsoftonline.com/{self.tenant_id}/v2.0",
                options={"require": ["sub", "tid", "iat", "exp", "aud", "iss"]},
            )
        except pyjwt.PyJWTError as e:
            raise EntraError(f"id_token validation failed: {e}") from e

        if claims["tid"] != self.tenant_id:
            raise EntraError(
                f"id_token tid {claims['tid']} does not match expected {self.tenant_id}"
            )

        return EntraIdentity(
            sub=claims["sub"],
            tid=claims["tid"],
            email=claims.get("email"),
            name=claims.get("name"),
            preferred_username=claims.get("preferred_username"),
        )
