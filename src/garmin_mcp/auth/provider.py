"""OAuth 2.1 authorization-server-provider implementation.

Bridges Claude clients (which speak DCR + OAuth) to Microsoft Entra ID
(which doesn't support DCR). Sequence:

    Claude → /register             → register_client (we issue our client_id)
    Claude → /authorize?...        → authorize() returns an Entra URL
    user signs in to Entra
    Entra → /callback?code,state   → complete_authorization() exchanges with
                                     Entra, persists the user, mints our own
                                     auth code, returns the Claude redirect URL
    Claude → /token (with our code) → exchange_authorization_code() returns
                                      our JWT access token + refresh token
    Claude → /mcp (Bearer JWT)     → TokenVerifier validates our JWT (separate
                                      module)
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from garmin_mcp.auth.audit import AuditLog
from garmin_mcp.auth.entra import EntraError, EntraOIDCClient
from garmin_mcp.auth.garmin_tokens import GarminTokenStore
from garmin_mcp.auth.jwt import JwtSigner
from garmin_mcp.auth.onboarding import OnboardingManager
from garmin_mcp.auth.storage import Storage
from garmin_mcp.auth.throttle import RegistrationGuard
from garmin_mcp.user_context import register_ip, set_current_user_id


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_secret(secret: str) -> str:
    # The secret is high-entropy (we generate it ourselves), so plain SHA-256
    # is appropriate; we don't need bcrypt's slowness for non-user-chosen secrets.
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class GarminMcpProvider(OAuthAuthorizationServerProvider):
    """Bridges Claude clients to Entra ID."""

    def __init__(
        self,
        storage: Storage,
        entra: EntraOIDCClient,
        jwt_signer: JwtSigner,
        registration_guard: RegistrationGuard,
        audit: AuditLog,
        garmin_tokens: GarminTokenStore | None = None,
        onboarding: OnboardingManager | None = None,
        public_url: str | None = None,
        default_scopes: list[str] | None = None,
        access_token_ttl_seconds: int = 3600,
        refresh_token_ttl_seconds: int = 30 * 24 * 3600,
    ):
        # `garmin_tokens` and `onboarding` are required together: if a user
        # signs in via Entra but has no stored Garmin tokens, we must redirect
        # them through the onboarding flow before issuing the OAuth code.
        # Both being None is the legacy single-Garmin-account mode used in
        # Step 4 (every authenticated user shares one Garmin login).
        if (garmin_tokens is None) != (onboarding is None):
            raise ValueError("garmin_tokens and onboarding must be provided together")
        self.storage = storage
        self.entra = entra
        self.jwt_signer = jwt_signer
        self.guard = registration_guard
        self.audit = audit
        self.garmin_tokens = garmin_tokens
        self.onboarding = onboarding
        self._public_url = (public_url or "").rstrip("/")
        self._default_scopes = default_scopes or ["mcp.use"]
        self._access_ttl = access_token_ttl_seconds
        self._refresh_ttl = refresh_token_ttl_seconds

    # DCR (RFC 7591) -------------------------------------------------------

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Field-level hygiene
        if not self.guard.check_field_lengths(client_info.model_dump(mode="json")):
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description="field exceeds max length",
            )
        for uri in client_info.redirect_uris or []:
            if not self.guard.check_redirect_uri(str(uri)):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"redirect_uri {uri} must be HTTPS or localhost",
                )

        # Per-IP rate limit.
        rip = register_ip.get()
        if rip and not await self.guard.check_per_ip(rip):
            raise RegistrationError(
                error="server_error",  # type: ignore[arg-type]
                error_description="rate limit exceeded; try again later",
            )

        # Global cap
        if not self.guard.under_global_cap():
            raise RegistrationError(
                error="server_error",  # type: ignore[arg-type]
                error_description="registration capacity reached; try again later",
            )

        # Generate id (and optional secret for confidential clients)
        client_id = secrets.token_urlsafe(24)
        client_secret = None
        client_secret_hash = None
        if (client_info.token_endpoint_auth_method or "client_secret_basic") != "none":
            client_secret = secrets.token_urlsafe(32)
            client_secret_hash = _hash_secret(client_secret)

        client_info.client_id = client_id
        client_info.client_secret = client_secret
        client_info.client_id_issued_at = int(time.time())
        client_info.client_secret_expires_at = 0  # 0 = never

        self.storage.register_client(
            client_id=client_id,
            client_secret_hash=client_secret_hash,
            client_metadata=client_info.model_dump(mode="json"),
            register_ip=rip,
        )
        self.audit.record(
            "register.success",
            client_id=client_id,
            client_name=client_info.client_name,
        )

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self.storage.get_client(client_id)
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate(row["client_metadata"])

    # Authorization (Claude → Entra → us) ----------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        state = secrets.token_urlsafe(24)
        self.storage.store_pending_authorization(
            state=state,
            client_id=client.client_id or "",
            claude_redirect_uri=str(params.redirect_uri),
            claude_state=params.state,
            code_challenge=params.code_challenge,
            code_challenge_method="S256",
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            scopes=params.scopes or self._default_scopes,
            resource=params.resource,
        )
        self.audit.record(
            "authorize.start",
            client_id=client.client_id,
            scopes=" ".join(params.scopes or self._default_scopes),
        )
        return await self.entra.authorization_url(state=state)

    async def complete_authorization(self, state: str, entra_code: str) -> str:
        """Called by the /callback Starlette route after Entra redirects back.

        Returns a URL to redirect the browser to. For users with Garmin
        tokens already on file this is Claude's redirect_uri with our auth
        code; for first-time users it's `/onboard?ticket=...` and the OAuth
        completion is deferred until onboarding succeeds.
        """
        pending = self.storage.consume_pending_authorization(state)
        if pending is None:
            self.audit.record("authorize.callback", outcome="unknown_state")
            raise TokenError(
                error="invalid_request",
                error_description="unknown or expired state",
            )

        try:
            identity = await self.entra.exchange_code(entra_code)
        except EntraError as e:
            self.audit.record(
                "authorize.callback",
                outcome="entra_error",
                state=state,
                detail=str(e),
            )
            raise TokenError(
                error="server_error",  # type: ignore[arg-type]
                error_description="server error",
            )

        user = self.storage.get_or_create_user(
            user_id_factory=lambda: str(uuid.uuid4()),
            entra_sub=identity.sub,
            entra_tid=identity.tid,
            email=identity.email,
            display_name=identity.name or identity.preferred_username,
        )

        # If this server is wired for multi-user Garmin (i.e. has a token
        # store + onboarding manager) and the user hasn't onboarded yet,
        # detour through the onboarding flow before issuing the OAuth code.
        if (
            self.garmin_tokens is not None
            and self.onboarding is not None
            and not self.garmin_tokens.has(user["user_id"])
        ):
            session = self.onboarding.create_session(
                user_id=user["user_id"],
                on_success=lambda uid: self._issue_code_for(pending, uid),
            )
            self.audit.record(
                "authorize.callback",
                outcome="needs_onboarding",
                client_id=pending["client_id"],
                user_id=user["user_id"],
            )
            return f"{self._public_url}/onboard?ticket={session.ticket}"

        # User has Garmin tokens (or we're in single-account mode) — issue
        # our auth code immediately and bounce back to Claude.
        redirect = self._issue_code_for(pending, user["user_id"])
        self.audit.record(
            "authorize.callback",
            outcome="ok",
            client_id=pending["client_id"],
            user_id=user["user_id"],
        )
        return redirect

    def _issue_code_for(self, pending: dict, user_id: str) -> str:
        """Mint our auth code, persist it, and return the Claude redirect URL.

        Called either directly from `complete_authorization` (existing user)
        or from the onboarding manager's `on_success` callback (new user
        finishing the Garmin login)."""
        our_code = secrets.token_urlsafe(24)
        self.storage.store_authorization_code(
            code=our_code,
            client_id=pending["client_id"],
            user_id=user_id,
            redirect_uri=pending["claude_redirect_uri"],
            redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            code_challenge=pending["code_challenge"],
            scopes=pending["scopes"],
            resource=pending["resource"],
        )
        params = {"code": our_code}
        if pending["claude_state"]:
            params["state"] = pending["claude_state"]
        sep = "&" if "?" in pending["claude_redirect_uri"] else "?"
        return f"{pending['claude_redirect_uri']}{sep}{urlencode(params)}"

    # Authorization code → token --------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.storage.load_authorization_code(authorization_code)
        if row is None or row["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=row["code"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=AnyUrl(row["redirect_uri"]),
            redirect_uri_provided_explicitly=row["redirect_uri_provided_explicitly"],
            resource=row["resource"],
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        row = self.storage.consume_authorization_code(authorization_code.code)
        if row is None or row["client_id"] != client.client_id:
            raise TokenError(
                error="invalid_grant",
                error_description="unknown or already-used code",
            )

        return self._mint_token_pair(
            client_id=client.client_id or "",
            user_id=row["user_id"],
            scopes=row["scopes"],
        )

    # Refresh token ---------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self.storage.load_refresh_token(_hash_token(refresh_token))
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"],
            expires_at=row["expires_at"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        token_hash = _hash_token(refresh_token.token)
        row = self.storage.load_refresh_token(token_hash)
        if row is None or row["client_id"] != client.client_id:
            raise TokenError(error="invalid_grant", error_description="unknown refresh token")
        # Rotate: revoke the old token, issue a fresh pair.
        self.storage.revoke_refresh_token(token_hash)

        # If the client narrowed scopes, honor that; otherwise reuse stored set.
        granted_scopes = scopes or row["scopes"]
        for s in granted_scopes:
            if s not in row["scopes"]:
                raise TokenError(
                    error="invalid_scope",
                    error_description=f"scope {s} not in original grant",
                )

        return self._mint_token_pair(
            client_id=client.client_id or "",
            user_id=row["user_id"],
            scopes=granted_scopes,
        )

    # Access tokens (loaded by FastMCP's resource-server side) -------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        # JWTs are self-contained — no DB lookup, no denylist (tokens are
        # short-lived; only refresh tokens are revoked).
        # FastMCP calls this on every authenticated request, so this is the
        # right place to set the per-request user_id ContextVar that tools
        # consult via `get_garmin_client()`.
        try:
            claims = self.jwt_signer.verify(token)
        except Exception:
            return None
        set_current_user_id(claims.user_id)
        return AccessToken(
            token=token,
            client_id=claims.client_id,
            scopes=claims.scopes,
            expires_at=claims.expires_at,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Only refresh tokens are stateful; access tokens just expire.
        if isinstance(token, RefreshToken):
            self.storage.revoke_refresh_token(_hash_token(token.token))

    # Helpers --------------------------------------------------------------

    def _mint_token_pair(self, client_id: str, user_id: str, scopes: list[str]) -> OAuthToken:
        access = self.jwt_signer.issue(
            user_id=user_id,
            client_id=client_id,
            scopes=scopes,
            ttl_seconds=self._access_ttl,
        )
        refresh = secrets.token_urlsafe(48)
        self.storage.store_refresh_token(
            token_hash=_hash_token(refresh),
            client_id=client_id,
            user_id=user_id,
            scopes=scopes,
            expires_at=int(time.time()) + self._refresh_ttl,
        )
        self.storage.mark_client_used(client_id)
        self.audit.record("token.issued", client_id=client_id, user_id=user_id)
        return OAuthToken(
            access_token=access.token,
            token_type="Bearer",
            expires_in=self._access_ttl,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )
